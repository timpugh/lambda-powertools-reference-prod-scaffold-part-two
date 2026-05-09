"""Integration tests for the API Gateway endpoint.

These tests require a deployed stack. They are skipped automatically when the
stack cannot be found, so the standard ``pytest`` run (unit tests) stays green
without a live deployment. To run integration tests explicitly:

    pytest tests/integration/ -c region=us-east-1

The stack name is read from the ``AWS_BACKEND_STACK_NAME`` environment variable
(set in pyproject.toml). Override it for a different region:

    AWS_BACKEND_STACK_NAME=HelloWorld-ap-southeast-1 pytest tests/integration/
"""

import os
import uuid

import boto3
import pytest
import requests


def _idempotency_headers() -> dict[str, str]:
    """Fresh Idempotency-Key per call so each request is treated as new.

    A real client should reuse the same key across automatic retries of one
    logical request; tests just need uniqueness so replay-cache hits don't
    confound assertions.
    """
    return {"Idempotency-Key": str(uuid.uuid4())}


class TestApiGateway:
    @pytest.fixture
    def api_gateway_url(self):
        """Get the API Gateway URL from CloudFormation stack outputs.

        Skips the test if the stack is not deployed rather than failing, so
        the test suite stays green in environments without a live deployment.
        """
        stack_name = os.environ.get("AWS_BACKEND_STACK_NAME")

        if stack_name is None:
            pytest.skip("AWS_BACKEND_STACK_NAME not set — skipping integration tests")

        client = boto3.client("cloudformation")

        try:
            response = client.describe_stacks(StackName=stack_name)
        except Exception:
            pytest.skip(f"Stack '{stack_name}' not found — skipping integration tests")

        stacks = response["Stacks"]
        stack_outputs = stacks[0]["Outputs"]
        api_outputs = [output for output in stack_outputs if output["OutputKey"] == "HelloWorldApiOutput"]

        if not api_outputs:
            pytest.skip(f"HelloWorldApiOutput not found in stack '{stack_name}' — skipping integration tests")

        return api_outputs[0]["OutputValue"]

    def test_api_gateway(self, api_gateway_url):
        """Call the API Gateway endpoint and check the response"""
        response = requests.get(api_gateway_url, timeout=10, headers=_idempotency_headers())

        assert response.status_code == 200
        assert response.json() == {"message": "hello world"}

    def test_api_gateway_response_headers(self, api_gateway_url):
        """Verify the response returns correct content type"""
        response = requests.get(api_gateway_url, timeout=10, headers=_idempotency_headers())

        assert response.headers["Content-Type"] == "application/json"

    def test_api_gateway_response_time_warm(self, api_gateway_url):
        """Warm-path latency budget — a P50 ceiling, not a timeout proxy.

        A first call warms the container; a second call asserts the warm-path
        budget. ``response.elapsed`` is the request-side timing as measured by
        ``requests`` (network + server), so this is a black-box ceiling rather
        than a backend SLO.
        """
        # warm-up
        requests.get(api_gateway_url, timeout=10, headers=_idempotency_headers())
        response = requests.get(api_gateway_url, timeout=10, headers=_idempotency_headers())

        assert response.status_code == 200
        assert response.elapsed.total_seconds() < 2.0

    def test_missing_idempotency_key_returns_400(self, api_gateway_url):
        """The Lambda requires Idempotency-Key — calls without it return 400."""
        response = requests.get(api_gateway_url, timeout=10)

        assert response.status_code == 400
        assert "Idempotency-Key" in response.text
