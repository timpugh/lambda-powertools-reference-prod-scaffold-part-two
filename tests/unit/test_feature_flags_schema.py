"""Validate the AppConfig feature-flag content against the Powertools schema.

The flag JSON lives in ``infrastructure/feature_flags.json`` — one file read by
the CDK construct at synth time (which can only ``json.loads`` it, because
Powertools is not installable next to aws-cdk-lib; see the attrs conflict in
pyproject.toml) and by this test, which runs in the Lambda-side venv where
Powertools *is* importable and can enforce the real schema.

This split exists because a schema-invalid flag is invisible until runtime:
the handler's fallback path swallows the SchemaValidationError and quietly
evaluates every flag to its default — exactly how a malformed configuration
once survived synth, tests, and deploy before surfacing on a live stack.
Failing here puts the guard at commit time.
"""

import json
from pathlib import Path

import pytest

# Powertools lives in .venv-lambda only. The other unit tests reach it through
# the lazy lambda_app_module fixture so collection stays clean in the CDK-side
# .venv (cdk-check CI job, VS Code root-folder test discovery); this module
# imports it directly, so it needs the same guard tests/cdk uses — skip, don't
# error, where Powertools is absent. The assignment form (rather than re-import
# statements after the guard) keeps ruff's E402 check enabled for this file.
_feature_flags = pytest.importorskip(
    "aws_lambda_powertools.utilities.feature_flags",
    reason="Powertools not installed — this suite runs in .venv-lambda",
)
SchemaValidator = _feature_flags.SchemaValidator
SchemaValidationError = _feature_flags.exceptions.SchemaValidationError

FLAGS_PATH = Path(__file__).resolve().parents[2] / "infrastructure" / "feature_flags.json"


def test_feature_flags_file_matches_powertools_schema():
    """The committed flag file must satisfy the Powertools feature-flags schema.

    SchemaValidator is the same validation the Powertools FeatureFlags store
    applies at fetch time — passing here means the deployed configuration is
    parseable by the handler, not merely valid JSON.
    """
    flags = json.loads(FLAGS_PATH.read_text())

    SchemaValidator(flags).validate()


def test_expected_flag_present_with_safe_default():
    """enhanced_greeting must exist and default to False.

    The handler treats the flag's default as the safe state; flipping the
    committed default to True would silently enable the feature for every
    caller on the next deploy.
    """
    flags = json.loads(FLAGS_PATH.read_text())

    assert flags["enhanced_greeting"]["default"] is False


def test_schema_validator_rejects_native_appconfig_format():
    """Guard the format confusion this file exists to prevent.

    AppConfig's native feature-flags profile serves ``{"flag": {"enabled":
    bool}}`` at the data plane — a shape Powertools rejects. This test pins
    that the validator genuinely distinguishes the two formats, so a future
    edit that pastes native-format JSON into feature_flags.json cannot pass
    the schema test above by accident.
    """
    native_format = {"enhanced_greeting": {"enabled": False}}

    with pytest.raises(SchemaValidationError):
        SchemaValidator(native_format).validate()
