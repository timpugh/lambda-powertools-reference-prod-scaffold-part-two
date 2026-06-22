"""Unit tests for the Lambda handler."""

import json

import pytest


def test_lambda_handler(apigw_event, lambda_context, lambda_app_module):
    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    data = json.loads(ret["body"])

    assert ret["statusCode"] == 200
    assert "message" in ret["body"]
    assert data["message"] == "hello world"


def test_lambda_handler_returns_valid_json(apigw_event, lambda_context, lambda_app_module):
    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    body = json.loads(ret["body"])
    assert isinstance(body, dict)


def test_lambda_handler_status_code(apigw_event, lambda_context, lambda_app_module):
    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    assert ret["statusCode"] == 200


def test_enhanced_greeting_feature_flag(apigw_event, lambda_context, lambda_app_module, mocker):
    """Test that enhanced greeting feature flag changes the response."""
    mocker.patch.object(lambda_app_module.feature_flags, "evaluate", return_value=True)

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    data = json.loads(ret["body"])

    # Assert the full composed string, not just the suffix, so a change to the
    # base greeting or the separator is caught (the SSM mock pins "hello world").
    assert data["message"] == "hello world - enhanced mode enabled"


def test_feature_flag_receives_ip_and_user_agent_context(apigw_event, lambda_context, lambda_app_module, mocker):
    """The handler must pass source_ip + user_agent context to feature_flags.evaluate.

    The /greeting route documents IP-based gating of enhanced_greeting; without the
    context dict the AppConfig rules engine can never see those values, so this
    pins the contract. Dropping the context= arg would otherwise pass the suite.
    """
    spy = mocker.patch.object(lambda_app_module.feature_flags, "evaluate", return_value=False)

    lambda_app_module.lambda_handler(apigw_event, lambda_context)

    spy.assert_called_once_with(
        name="enhanced_greeting",
        context={"source_ip": "127.0.0.1", "user_agent": "Custom User Agent String"},
        default=False,
    )


def test_retry_config_wired_into_sdk_clients(lambda_app_module):
    """The shared adaptive retry boto_config must reach every SDK client.

    A refactor dropping boto_config= from any of the three constructors would
    silently regress the documented retry posture while passing every other test.
    """
    assert lambda_app_module.boto_config.retries["mode"] == "adaptive"
    # SSMProvider and the DynamoDB persistence layer both build their clients from
    # the shared config; assert the live client config reflects adaptive mode.
    assert lambda_app_module.ssm_provider.client.meta.config.retries["mode"] == "adaptive"
    assert lambda_app_module.persistence_layer.client.meta.config.retries["mode"] == "adaptive"


def test_ssm_failure_returns_500(apigw_event, lambda_context, lambda_app_module, mocker):
    """Test that an SSM parameter fetch failure returns a 500 response.

    The handler catches Powertools' GetParameterError and raises
    InternalServerError, which becomes a 500 API Gateway response. Truly
    unexpected exception types intentionally propagate to Powertools' default
    handler so they surface correctly in metrics and X-Ray.
    """
    from aws_lambda_powertools.utilities.parameters.exceptions import GetParameterError

    mocker.patch.object(
        lambda_app_module.ssm_provider,
        "get",
        side_effect=GetParameterError("SSM unavailable"),
    )

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 500


def test_feature_flag_failure_falls_back_to_default(apigw_event, lambda_context, lambda_app_module, mocker, caplog):
    """Test that a feature flag evaluation failure falls back gracefully.

    AppConfig failures are non-critical — the handler catches the Powertools
    FeatureFlags exception types (StoreClientError covers boto3 / network
    errors against the AppConfig data plane) and uses the default value
    (False) rather than failing the whole request.

    The fallback warning must carry the underlying exception (exc_info):
    without it the cause is invisible in CloudWatch and a permanently broken
    AppConfig integration looks identical to a transient blip — exactly how
    a real misconfiguration stayed hidden until the first live deploy.
    """
    from aws_lambda_powertools.utilities.feature_flags.exceptions import StoreClientError

    mocker.patch.object(
        lambda_app_module.feature_flags,
        "evaluate",
        side_effect=StoreClientError("AppConfig unavailable"),
    )

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    data = json.loads(ret["body"])

    assert ret["statusCode"] == 200
    assert data["message"] == "hello world"
    # The warning record must carry the underlying exception (exc_info), not
    # just the generic message. caplog (not capsys) because Powertools'
    # stdout handler binds the session-level stream, which per-test capsys
    # never sees; the record itself still propagates to pytest's capture.
    records = [r for r in caplog.records if "Feature flag evaluation failed" in r.getMessage()]
    assert records, "expected the fallback warning to be logged"
    assert records[0].exc_info is not None, "fallback warning must include exc_info"
    assert records[0].exc_info[0].__name__ == "StoreClientError"
    assert "AppConfig unavailable" in str(records[0].exc_info[1])


def test_unknown_route_returns_404(apigw_event, lambda_context, lambda_app_module):
    """Test that a request to an unknown route returns 404."""
    apigw_event["path"] = "/unknown"
    apigw_event["resource"] = "/unknown"

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 404


def test_unsupported_method_returns_404(apigw_event, lambda_context, lambda_app_module):
    """Test that an unsupported HTTP method returns 404.

    Powertools APIGatewayRestResolver returns 404 (not 405) for method+path
    combinations that have no registered route handler.
    """
    apigw_event["httpMethod"] = "POST"

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 404


def test_missing_idempotency_key_returns_400(apigw_event, lambda_context, lambda_app_module, monkeypatch):
    """A request without an Idempotency-Key header is rejected with 400.

    The header is a hard requirement — without it Powertools' @idempotent
    layer raises IdempotencyKeyError, which the handler converts to a 400
    response so callers see a meaningful error instead of an unhandled 500.

    POWERTOOLS_IDEMPOTENCY_DISABLED is normally set in pytest_env so the
    other tests don't hit DynamoDB; for this assertion specifically we
    re-enable the layer so the missing-key path actually executes.
    """
    monkeypatch.delenv("POWERTOOLS_IDEMPOTENCY_DISABLED", raising=False)
    del apigw_event["headers"]["Idempotency-Key"]

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 400
    assert "Idempotency-Key" in ret["body"]
    # The 400 is built by hand outside the Powertools resolver, so it must carry
    # its own CORS header — without it, cross-origin browser callers (the
    # CloudFront-hosted frontend) get an opaque CORS failure instead of the
    # documented 400 body. Keep in sync with CORSConfig.allow_origin.
    assert ret["headers"]["Access-Control-Allow-Origin"] == "*"


def test_missing_headers_object_returns_400(apigw_event, lambda_context, lambda_app_module, monkeypatch):
    """An event with no headers map at all is rejected with 400, not a crash.

    Manual invocations (console test events, aws lambda invoke) can omit the
    headers key entirely; the handler's header normalization must tolerate
    that and the idempotency layer then rejects the key-less request cleanly.
    """
    monkeypatch.delenv("POWERTOOLS_IDEMPOTENCY_DISABLED", raising=False)
    del apigw_event["headers"]

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 400


@pytest.mark.parametrize(
    "header_name",
    ["idempotency-key", "Idempotency-Key", "IDEMPOTENCY-KEY", "Idempotency-keY"],
)
def test_idempotency_key_header_is_case_insensitive(
    header_name, apigw_event, lambda_context, lambda_app_module, monkeypatch, mocker
):
    """Any casing of the Idempotency-Key header is accepted.

    HTTP header names are case-insensitive (RFC 9110) and API Gateway preserves
    the casing the caller sent, but JMESPath lookups are exact-match — so the
    handler lowercases header keys before the idempotency layer sees the event.
    POWERTOOLS_IDEMPOTENCY_DISABLED is unset for this test so the @idempotent
    decorator actually evaluates the JMESPath rather than short-circuiting —
    otherwise the test passes trivially regardless of which header is present.
    The persistence layer's mutating methods are mocked to no-ops so the test
    never touches DynamoDB; ``_get_remaining_time_in_millis`` is also patched
    so Powertools doesn't try to compute a timedelta from a MagicMock context.
    """
    monkeypatch.delenv("POWERTOOLS_IDEMPOTENCY_DISABLED", raising=False)
    mocker.patch(
        "aws_lambda_powertools.utilities.idempotency.base.IdempotencyHandler._get_remaining_time_in_millis",
        return_value=30_000,
    )
    mocker.patch.object(lambda_app_module.persistence_layer, "_put_record", return_value=None)
    mocker.patch.object(lambda_app_module.persistence_layer, "_get_record", side_effect=Exception("not found"))
    mocker.patch.object(lambda_app_module.persistence_layer, "_update_record", return_value=None)
    mocker.patch.object(lambda_app_module.persistence_layer, "_delete_record", return_value=None)
    del apigw_event["headers"]["Idempotency-Key"]
    apigw_event["headers"][header_name] = "test-idempotency-key-casing"

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 200


def test_env_model_rejects_missing_variable(lambda_app_module):
    """EnvVars fails validation when a required variable is absent.

    The model is validated at import time on real deploys so a missing var
    fails the cold start with a field-by-field pydantic report; this test
    pins that contract by validating an env dict with one key removed.
    """
    from pydantic import ValidationError

    valid = dict.fromkeys(lambda_app_module.EnvVars.model_fields, "test-value")
    del valid["IDEMPOTENCY_TABLE_NAME"]

    with pytest.raises(ValidationError, match="IDEMPOTENCY_TABLE_NAME"):
        lambda_app_module.EnvVars.model_validate(valid)


def test_env_model_rejects_empty_string(lambda_app_module):
    """EnvVars rejects empty strings, not just absent keys.

    An env var that is *set but empty* (a common CDK wiring mistake — e.g. an
    unresolved token rendering as "") must fail validation the same way a
    missing one does, rather than flowing into boto3 as an empty table name.
    """
    from pydantic import ValidationError

    values = dict.fromkeys(lambda_app_module.EnvVars.model_fields, "test-value")
    values["GREETING_PARAM_NAME"] = ""

    with pytest.raises(ValidationError, match="GREETING_PARAM_NAME"):
        lambda_app_module.EnvVars.model_validate(values)


def test_env_model_appconfig_max_age_default_and_wiring(lambda_app_module):
    """APPCONFIG_MAX_AGE_SECONDS defaults to 300 and reaches the AppConfig store.

    The default keeps the feature-flag fetch on the same 300s caching posture
    as the SSM read; without max_age wired through, Powertools re-polls the
    AppConfig data plane every 5 seconds per warm container. Asserting on the
    live store instance catches a refactor that drops the parameter while the
    model keeps validating.
    """
    values = {f: "test-value" for f in lambda_app_module.EnvVars.model_fields if f != "APPCONFIG_MAX_AGE_SECONDS"}
    env = lambda_app_module.EnvVars.model_validate(values)

    assert env.APPCONFIG_MAX_AGE_SECONDS == 300
    assert lambda_app_module.app_config_store.cache_seconds == 300


def test_resolve_tenant_id_defaults_to_anonymous(apigw_event, lambda_app_module):
    """With no authorizer on the request, tenant context resolves to "anonymous".

    There is no authentication in this reference, so every request today takes
    this fallback path — it is the value that flows into logs, metrics, and the
    trace. Pinning it guards the default a fork relies on until it wires auth.
    """
    from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEvent

    event = APIGatewayProxyEvent(apigw_event)

    assert lambda_app_module._resolve_tenant_id(event) == "anonymous"


def test_resolve_tenant_id_reads_authorizer_claim(apigw_event, lambda_app_module):
    """A ``tenantId`` claim on the API Gateway authorizer context wins over the default.

    This is the forward-compatible path: when a fork adds a Cognito/JWT or custom
    Lambda authorizer, the tenant claim lands in ``requestContext.authorizer`` and
    every telemetry signal is already dimensioned by it — no retrofit needed.
    Sourcing from the authorizer (set server-side) rather than a client header is
    deliberate: a client-controlled value could be spoofed to read another
    tenant's telemetry.
    """
    from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEvent

    apigw_event["requestContext"]["authorizer"] = {"tenantId": "acme-corp"}
    event = APIGatewayProxyEvent(apigw_event)

    assert lambda_app_module._resolve_tenant_id(event) == "acme-corp"


def test_tenant_id_tags_structured_logs(apigw_event, lambda_context, lambda_app_module):
    """The resolved tenant_id appears on the structured log records of the request.

    This is the payoff a junior dev cares about: being able to ``filter
    tenant_id = ...`` in Logs Insights. Captures the Powertools logger's own
    stdout stream (per-test capsys never sees it — see tests/unit/conftest notes)
    and asserts the JSON log line carries the field.
    """
    import io

    logger = lambda_app_module.logger
    buf = io.StringIO()
    swapped = [h for h in logger.handlers if hasattr(h, "stream")]
    originals = [h.stream for h in swapped]
    for handler in swapped:
        handler.stream = buf
    try:
        lambda_app_module.lambda_handler(apigw_event, lambda_context)
    finally:
        for handler, original in zip(swapped, originals, strict=True):
            handler.stream = original

    assert '"tenant_id":"anonymous"' in buf.getvalue()


def test_tenant_id_added_as_metric_dimension(apigw_event, lambda_context, lambda_app_module, mocker):
    """tenant_id is added as a CloudWatch metric dimension for the invocation.

    The shared Powertools metric store means the service-layer metrics
    (GreetingRequests, FeatureFlagEvaluationFailure) inherit the dimension too,
    so they can be charted per tenant. A refactor dropping the dimension would
    otherwise pass every other test.
    """
    spy = mocker.patch.object(lambda_app_module.metrics, "add_dimension")

    lambda_app_module.lambda_handler(apigw_event, lambda_context)

    spy.assert_any_call(name="tenant_id", value="anonymous")


def test_tenant_id_added_as_trace_annotation(apigw_event, lambda_context, lambda_app_module, mocker):
    """tenant_id is added as a filterable X-Ray annotation for the invocation.

    Annotations (not metadata) are the indexed, queryable kind — so the X-Ray
    console can filter the browser→API Gateway→Lambda trace down to one tenant.
    """
    spy = mocker.patch.object(lambda_app_module.tracer, "put_annotation")

    lambda_app_module.lambda_handler(apigw_event, lambda_context)

    spy.assert_any_call(key="tenant_id", value="anonymous")


def test_persistence_layer_error_propagates(apigw_event, lambda_context, lambda_app_module, monkeypatch, mocker):
    """A DynamoDB-side persistence failure does not get masked as a 400.

    The outer handler intentionally only catches ``IdempotencyKeyError``
    (which has a meaningful 400 mapping); persistence-layer failures
    propagate up to the Lambda runtime instead, so the original exception
    type surfaces in CloudWatch metrics and X-Ray rather than being silently
    flattened into the generic 400 path. We assert the exception escapes
    rather than being absorbed.
    """
    from aws_lambda_powertools.utilities.idempotency.exceptions import IdempotencyPersistenceLayerError

    monkeypatch.delenv("POWERTOOLS_IDEMPOTENCY_DISABLED", raising=False)
    mocker.patch(
        "aws_lambda_powertools.utilities.idempotency.base.IdempotencyHandler._get_remaining_time_in_millis",
        return_value=30_000,
    )
    mocker.patch.object(
        lambda_app_module.persistence_layer,
        "_put_record",
        side_effect=IdempotencyPersistenceLayerError("DDB throttled", Exception("orig")),
    )

    with pytest.raises(IdempotencyPersistenceLayerError):
        lambda_app_module.lambda_handler(apigw_event, lambda_context)
