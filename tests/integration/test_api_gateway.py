"""Integration tests for the API Gateway endpoint.

The API is same-origin behind CloudFront: normal traffic goes through
``{cloudfront_url}/api/greeting``, and the regional WAF rejects callers that
hit the public execute-api URL directly (see ``test_direct_execute_api_is_blocked``).

These tests require a deployed stack. They are skipped automatically when the
stack cannot be found, so the standard ``pytest`` run (unit tests) stays green
without a live deployment. To run integration tests explicitly:

    make test-integration

The CloudFront-fronted tests read the ``AWS_FRONTEND_STACK_NAME`` environment
variable (set in pyproject.toml); the direct-URL 403 test reads
``AWS_BACKEND_STACK_NAME``. Override either for a different region:

    AWS_FRONTEND_STACK_NAME=ServerlessAppFrontend-ap-southeast-1 pytest tests/integration/
"""

import os
import uuid

import pytest
import requests

# boto3 lives in the `lambda` dependency group (.venv-lambda), not the CDK-side
# .venv — same split the unit conftest and tests/cdk handle with lazy loading /
# importorskip. Without this guard, VS Code's Testing panel (which discovers
# tests/ under the root .venv interpreter) fails collection with
# "Missing Module: boto3" instead of listing this suite as skipped.
boto3 = pytest.importorskip("boto3", reason="boto3 not installed — integration tests run in .venv-lambda")


def _idempotency_headers() -> dict[str, str]:
    """Fresh Idempotency-Key per call so each request is treated as new.

    A real client should reuse the same key across automatic retries of one
    logical request; tests just need uniqueness so replay-cache hits don't
    confound assertions.
    """
    return {"Idempotency-Key": str(uuid.uuid4())}


class TestApiGateway:
    @pytest.fixture
    def cloudfront_api_url(self):
        """Get the same-origin API URL from the frontend stack's CloudFront output.

        Skips the test if the stack is not deployed rather than failing, so
        the test suite stays green in environments without a live deployment.
        """
        stack_name = os.environ.get("AWS_FRONTEND_STACK_NAME")

        if stack_name is None:
            pytest.skip("AWS_FRONTEND_STACK_NAME not set — skipping integration tests")

        client = boto3.client("cloudformation")

        try:
            response = client.describe_stacks(StackName=stack_name)
        except Exception:
            pytest.skip(f"Stack '{stack_name}' not found — skipping integration tests")

        stacks = response["Stacks"]
        stack_outputs = stacks[0]["Outputs"]
        domain_outputs = [output for output in stack_outputs if output["OutputKey"] == "CloudFrontDomainName"]

        if not domain_outputs:
            pytest.skip(f"CloudFrontDomainName not found in stack '{stack_name}' — skipping integration tests")

        # OutputValue is already an https:// URL (see CfnOutput in frontend_stack.py).
        return f"{domain_outputs[0]['OutputValue']}/api/greeting"

    @pytest.fixture
    def api_gateway_url(self):
        """Get the public execute-api URL from the backend stack outputs.

        Used only by the direct-URL 403 test below — every other test in this
        class goes through CloudFront (see ``cloudfront_api_url``). Skips the
        test if the stack is not deployed rather than failing, so the test
        suite stays green in environments without a live deployment.
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
        api_outputs = [output for output in stack_outputs if output["OutputKey"] == "ApiUrlOutput"]

        if not api_outputs:
            pytest.skip(f"ApiUrlOutput not found in stack '{stack_name}' — skipping integration tests")

        return api_outputs[0]["OutputValue"]

    def test_api_gateway(self, cloudfront_api_url):
        """Call the same-origin API endpoint through CloudFront and check the response"""
        response = requests.get(cloudfront_api_url, timeout=10, headers=_idempotency_headers())

        assert response.status_code == 200
        assert response.json() == {"message": "hello world"}

    def test_api_gateway_response_headers(self, cloudfront_api_url):
        """Verify the response returns correct content type"""
        response = requests.get(cloudfront_api_url, timeout=10, headers=_idempotency_headers())

        assert response.headers["Content-Type"] == "application/json"

    def test_api_gateway_response_time_warm(self, cloudfront_api_url):
        """Warm-path latency budget — a P50 ceiling, not a timeout proxy.

        A black-box smoke check, not a backend SLO: ``response.elapsed`` is the
        request-side timing measured by ``requests`` (network + server). To reduce
        flakiness, a connection-reusing Session is warmed once, then the assertion
        uses the MIN of several samples (best-case warm latency) rather than a
        single measurement that a one-off network blip could fail. A failure here
        is a latency signal worth investigating, not necessarily a code defect.
        The request now makes an extra hop through CloudFront (same-origin
        routing) before reaching API Gateway; the 2.0s budget already has
        headroom for that, so it stays unchanged.
        """
        with requests.Session() as session:
            # Warm the container and the TCP/TLS connection.
            session.get(cloudfront_api_url, timeout=10, headers=_idempotency_headers())

            samples = []
            last_status = None
            for _ in range(3):
                resp = session.get(cloudfront_api_url, timeout=10, headers=_idempotency_headers())
                last_status = resp.status_code
                samples.append(resp.elapsed.total_seconds())

        assert last_status == 200
        assert min(samples) < 2.0, f"warm-path latency over budget: min={min(samples):.3f}s of {samples}"

    def test_missing_idempotency_key_returns_400(self, cloudfront_api_url):
        """The Lambda requires Idempotency-Key — calls without it return 400."""
        response = requests.get(cloudfront_api_url, timeout=10)

        assert response.status_code == 400
        assert "Idempotency-Key" in response.text

    def test_direct_execute_api_is_blocked(self, api_gateway_url):
        """The origin-lockdown proof: the public execute-api URL rejects callers
        that don't arrive through CloudFront (regional WAF RejectNonCloudFront)."""
        response = requests.get(api_gateway_url, timeout=10, headers=_idempotency_headers())
        assert response.status_code == 403
