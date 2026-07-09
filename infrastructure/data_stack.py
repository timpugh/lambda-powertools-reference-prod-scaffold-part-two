"""DataStack — the stateful data layer (DynamoDB + its own CMK).

Isolated from the compute/backend stack on purpose, per the CDK best practice
"keep stateful resources in their own stack." The payoff is lifecycle
independence: a fork can destroy and recreate the stateless compute stack
freely without risking the data, and can switch on retention with one flag.

**Why this exists in a template that ships destroy-friendly.** Stack topology
is the expensive-to-retrofit decision; ``RemovalPolicy.RETAIN`` is a one-line
flag. So the *structure* (this dedicated stack) is baked in now, and the only
thing a production fork must change is ``retain_data=True`` — at which point
the table and its key flip to ``RETAIN``, DynamoDB deletion protection turns
on, and the stack gets termination protection. The default is ``False`` so
development and ephemeral environments tear down cleanly.

**Dedicated CMK (not the compute stack's key).** The table is encrypted with
*this* stack's key. Keeping the key with the data it protects is what makes
retention meaningful — a retained table whose key lived in a destroyable
compute stack would be unreadable after that stack is torn down. It also keeps
the cross-stack surface to a single relationship (the Lambda's grant on the
table), avoids sharing a key across the stack boundary, and gives each key a
tighter, least-privilege key policy.
"""

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_backup as backup
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from constructs import Construct

from infrastructure.nag_utils import acknowledge_rules, apply_compliance_aspects


class DataStack(Stack):
    """Stateful data layer: the Powertools idempotency table and its CMK.

    Exposes ``idempotency_table`` (and ``encryption_key``) for the compute
    stack to consume cross-stack — the Lambda receives the table to wire its
    ``IDEMPOTENCY_TABLE_NAME`` env var and a scoped read/write grant.
    """

    def __init__(self, scope: Construct, construct_id: str, *, retain_data: bool = False, **kwargs: Any) -> None:
        """Build the data stack.

        Args:
            scope: The CDK construct scope.
            construct_id: The unique identifier for this stack.
            retain_data: Production switch. ``False`` (default) keeps everything
                ``RemovalPolicy.DESTROY`` with no deletion/termination
                protection so the stack tears down cleanly — the right default
                for a template and for ephemeral environments. ``True`` flips
                the table and CMK to ``RETAIN``, enables DynamoDB deletion
                protection, and turns on stack termination protection.
            **kwargs: Additional keyword arguments passed to the parent Stack.
        """
        # Termination protection rides the same flag: a production data stack
        # should refuse an accidental `cdk destroy` of the whole stack.
        super().__init__(scope, construct_id, termination_protection=retain_data, **kwargs)

        apply_compliance_aspects(self)

        removal_policy = RemovalPolicy.RETAIN if retain_data else RemovalPolicy.DESTROY

        # Dedicated CMK for the data layer — see the module docstring for why the
        # key lives with the data rather than being shared from the compute stack.
        self.encryption_key = kms.Key(
            self,
            "DataEncryptionKey",
            description=f"KMS key for {self.stack_name} DynamoDB",
            enable_key_rotation=True,
            rotation_period=Duration.days(90),
            removal_policy=removal_policy,
        )

        # DynamoDB table for Powertools idempotency. No table_name set — CDK
        # generates one, which avoids cross-deployment name collisions and
        # never blocks a replacement-style change.
        self.idempotency_table = dynamodb.TableV2(
            self,
            "IdempotencyTableV2",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="expiration",
            # On-demand billing — TableV2's equivalent of PAY_PER_REQUEST.
            billing=dynamodb.Billing.on_demand(),
            encryption=dynamodb.TableEncryptionV2.customer_managed_key(self.encryption_key),
            # THROTTLED_KEYS records contributor insights only for throttled
            # keys — the diagnostic signal this cache needs — at a fraction of
            # full-table insights cost.
            contributor_insights_specification=dynamodb.ContributorInsightsSpecification(
                enabled=True,
                mode=dynamodb.ContributorInsightsMode.THROTTLED_KEYS,
            ),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
                # Shortest PITR window AWS allows: records TTL out after an hour,
                # so day-old recovery points are pure storage cost.
                recovery_period_in_days=1,
            ),
            removal_policy=removal_policy,
            # Blocks even a manual table delete in production; off by default so
            # the dev/ephemeral teardown path stays clean.
            deletion_protection=retain_data,
        )

        CfnOutput(
            self,
            "IdempotencyTableName",
            description="DynamoDB table used for Lambda idempotency",
            value=self.idempotency_table.table_name,
        )

        if retain_data:
            # Production posture: AWS Backup on top of PITR. PITR's rolling window
            # (1 day here — records TTL out after an hour) covers oops-recovery;
            # AWS Backup covers the compliance horizon: daily backups kept 35 days
            # plus a monthly backup moved to cold storage and kept a year. The vault
            # uses this stack's CMK (key lives with the data — module docstring) and
            # is RETAINed like the table it protects. This retires the
            # DynamoDBInBackupPlan suppressions in the retain shape; the
            # destroy-friendly default keeps them below.
            vault = backup.BackupVault(
                self,
                "IdempotencyBackupVault",
                encryption_key=self.encryption_key,
                removal_policy=RemovalPolicy.RETAIN,
            )
            plan = backup.BackupPlan(
                self,
                "IdempotencyBackupPlan",
                backup_vault=vault,
                backup_plan_rules=[
                    backup.BackupPlanRule.daily(),
                    backup.BackupPlanRule.monthly1_year(),
                ],
            )
            plan.add_selection(
                "IdempotencyTableSelection",
                resources=[backup.BackupResource.from_dynamo_db_table(self.idempotency_table)],
            )
            # The selection's auto-created role uses the AWS-documented service policy.
            acknowledge_rules(
                plan,
                [
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": "AWSBackupServiceRolePolicyForBackup is the documented service role policy for AWS Backup selections",
                        "applies_to": [
                            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
                        ],
                    },
                ],
            )
        else:
            # PITR-only in the destroy-friendly default: the idempotency cache is
            # regenerable data; a backup plan would slow teardown for no recovery value.
            # Reason text renders into the committed snapshots' cdk_nag Metadata —
            # changing it requires a snapshot regen (UPDATE_SNAPSHOTS=1 make test-cdk).
            acknowledge_rules(
                self,
                [
                    {
                        "id": "NIST.800.53.R5-DynamoDBInBackupPlan",
                        "reason": "Destroy-friendly default — PITR covers the regenerable idempotency cache; retain_data=true adds the AWS Backup plan",
                    },
                    {
                        "id": "HIPAA.Security-DynamoDBInBackupPlan",
                        "reason": "Destroy-friendly default — PITR covers the regenerable idempotency cache; retain_data=true adds the AWS Backup plan",
                    },
                ],
            )
