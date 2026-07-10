"""Assertions over the standalone permissions-boundary CFN template.

The boundary is plain JSON (not CDK) so it can be deployed before the CDK
bootstrap roles exist. These tests pin the anti-escalation invariants; the
allow-list breadth is verified live (ephemeral deploy) per the spec.
"""

import json
from pathlib import Path

TEMPLATE = Path(__file__).parents[2] / "infrastructure" / "bootstrap" / "cdk-scaffold-boundary.json"
BOUNDARY_ARN_SUB = "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/cdk-scaffold-boundary"


def _statements() -> list[dict]:
    doc = json.loads(TEMPLATE.read_text())
    policy = doc["Resources"]["BoundaryPolicy"]["Properties"]
    assert policy["ManagedPolicyName"] == "cdk-scaffold-boundary"
    return policy["PolicyDocument"]["Statement"]


def _by_sid(sid: str) -> dict:
    matches = [s for s in _statements() if s.get("Sid") == sid]
    assert matches, f"statement {sid!r} missing"
    return matches[0]


def test_allow_list_covers_core_services() -> None:
    allow = _by_sid("AllowServiceActions")
    assert allow["Effect"] == "Allow"
    for prefix in (
        "cloudformation:*",
        "lambda:*",
        "iam:*",
        "sts:*",
        "codepipeline:*",
        "codebuild:*",
        "codeconnections:*",
    ):
        assert prefix in allow["Action"], f"{prefix} missing from boundary allow-list"


def test_boundary_policy_cannot_be_tampered_with() -> None:
    deny = _by_sid("DenyBoundaryPolicyTampering")
    assert deny["Effect"] == "Deny"
    assert set(deny["Action"]) == {
        "iam:CreatePolicyVersion",
        "iam:DeletePolicy",
        "iam:DeletePolicyVersion",
        "iam:SetDefaultPolicyVersion",
    }
    assert deny["Resource"] == {"Fn::Sub": BOUNDARY_ARN_SUB}


def test_boundary_cannot_be_removed_from_principals() -> None:
    deny = _by_sid("DenyBoundaryRemoval")
    assert set(deny["Action"]) == {
        "iam:DeleteRolePermissionsBoundary",
        "iam:DeleteUserPermissionsBoundary",
    }


def test_new_principals_require_the_boundary() -> None:
    for sid in ("DenyRoleCreationWithoutBoundary", "DenyBoundaryReplacement"):
        deny = _by_sid(sid)
        cond = deny["Condition"]["StringNotEquals"]["iam:PermissionsBoundary"]
        assert cond == {"Fn::Sub": BOUNDARY_ARN_SUB}
