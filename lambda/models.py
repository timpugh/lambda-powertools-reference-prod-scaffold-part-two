"""Model layer — Pydantic data contracts for the greeting API.

Kept separate from the handler (``app.py``) and the service layer
(``service.py``) so the request/response models and the typed env-var view can
be imported without pulling in the Powertools resolver or any AWS client
construction. The response models do triple duty: runtime request/response
validation (via the resolver's ``enable_validation``), the committed OpenAPI
spec (``scripts/generate_openapi.py``), and — for :class:`EnvVars` — the
import-time configuration check the handler runs at cold start.
"""

from typing import Annotated

from pydantic import BaseModel, Field, PositiveInt


class EnvVars(BaseModel):
    """Typed view of the environment variables the handler requires.

    Validated once at handler import (see ``app.py``). A missing or malformed
    env var (table name, profile name, a non-numeric cache age) would otherwise
    only surface deep inside boto3 as an opaque parameter-validation error;
    failing at import time with pydantic's field-by-field report makes the
    misconfiguration obvious in CloudWatch on the very first invocation.
    Validation is stricter than a presence check: empty strings are rejected
    and the cache age must parse as a positive integer.
    """

    IDEMPOTENCY_TABLE_NAME: Annotated[str, Field(min_length=1)]
    GREETING_PARAM_NAME: Annotated[str, Field(min_length=1)]
    APPCONFIG_APP_NAME: Annotated[str, Field(min_length=1)]
    APPCONFIG_ENV_NAME: Annotated[str, Field(min_length=1)]
    APPCONFIG_PROFILE_NAME: Annotated[str, Field(min_length=1)]
    # In-memory TTL for the fetched feature-flag configuration. Defaults to the
    # same 300s the SSM read uses (see ssm_provider.get in service.py) so the two
    # config-fetch paths share one caching posture; override per environment
    # via the Lambda environment block in CDK when flags must propagate faster.
    APPCONFIG_MAX_AGE_SECONDS: PositiveInt = 300


class GreetingResponse(BaseModel):
    """Response body for GET /greeting."""

    message: str = Field(
        ...,
        description="Greeting from SSM Parameter Store, optionally suffixed when the enhanced_greeting flag is on.",
        examples=["hello world", "hello world - enhanced mode enabled"],
    )


class MissingIdempotencyKeyResponse(BaseModel):
    """Body of the 400 returned when the required Idempotency-Key header is absent.

    Shape must match the hand-built response in ``lambda_handler`` — that 400 is
    constructed outside the Powertools resolver (the idempotency layer rejects
    the request before the resolver runs), so this model exists purely to
    document the contract in the generated OpenAPI spec.
    """

    message: str = Field(
        "Idempotency-Key header is required",
        description="Explanation of the rejected request.",
    )


class InternalErrorResponse(BaseModel):
    """Body of the 500 produced by Powertools' ServiceError handling.

    When ``get_greeting`` raises ``InternalServerError`` (e.g. the SSM read fails),
    the resolver serialises it as ``{"statusCode": 500, "message": ...}`` —
    documented here so spec consumers see the failure shape, not just the 200.
    """

    statusCode: int = Field(500, description="HTTP status code echoed in the body.")  # noqa: N815 — matches the wire format
    message: str = Field(..., description="Error description.", examples=["Failed to fetch greeting"])
