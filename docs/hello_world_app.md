# Hello World Application Construct

The `infrastructure.hello_world_app` module hosts the domain construct
(`HelloWorldApp`) that owns every backend resource — the KMS key, DynamoDB
idempotency table, SSM greeting parameter, AppConfig application/environment/
profile, the Lambda function, the API Gateway REST API, the CloudWatch
log groups, the monitoring facade, and the AppInsights cleanup custom
resource.

The thin [`HelloWorldStack`](cdk_stack.md) wrapper composes this construct
and attaches stack-level cdk-nag suppressions; everything else lives here.

## API reference

::: infrastructure.hello_world_app
