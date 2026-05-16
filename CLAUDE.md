# CLAUDE.md

Project-level instructions for future Claude Code sessions. Loaded on every session against this repo.

## Project

Reference architecture for serverless AWS Lambda + Powertools applications: three-stack CDK composition, five-rule-pack cdk-nag gating, end-to-end CMK encryption, WAF + CloudFront, CloudTrail data events, browser RUM with X-Ray correlation, Athena access-log analytics, and supply-chain hygiene. Designed to be forked via GitHub's "Use this template" — see the "Forking this template" section at the bottom.

## Environments — two venvs, never mix

CDK and Powertools require incompatible `attrs` versions (CDK pulls `attrs<26` via jsii; Powertools pulls `attrs>=26`). `[tool.uv.conflicts]` in `pyproject.toml` lets one `uv.lock` hold both resolutions, installed into separate venvs:

- `.venv` — CDK workstation. Used for `cdk synth`, `cdk deploy`, stack-assertion tests, lint/format/typecheck of `hello_world/`.
- `.venv-lambda` — Lambda runtime. Used for unit tests over `lambda/`, integration tests, the OpenAPI generator script.

`make install` provisions both. The Makefile uses `UV_PROJECT_ENVIRONMENT=.venv-lambda` to switch into the runtime env without an activation dance. Never install Powertools into `.venv` or CDK into `.venv-lambda`.

Run `make doctor` after `make install` to verify both venvs picked up the expected groups, `cdk`/`drawio` are on `PATH`, and pre-commit is wired. `make clean-venvs && make install` is the recovery path for a corrupted venv.

## `cdk synth` must use `'**'`

All three stacks live inside `HelloWorldStage` (a `cdk.Stage`). Bare `cdk synth` walks only the App's direct children, finds the Stage, doesn't recurse, and emits an empty synthesis that succeeds *without* running cdk-nag against the real stacks. `make cdk-synth` and the CI `cdk-check` job both invoke `cdk synth '**'`. If you run `cdk synth` directly during development, include the glob — otherwise the gate passes silently regardless of what cdk-nag would find.

## cdk-nag is a hard gate — and runs only via CLI synth, not assertion tests

Five rule packs run on every synth: AwsSolutions, Serverless, NIST 800-53 R5, HIPAA Security, PCI DSS 3.2.1. Findings fail CI. Resolve by:

1. **Fix the underlying issue** (preferred). README "Design decisions and known limitations" documents recurring patterns.
2. **Suppress with rationale**. Every suppression carries a `reason=` string. For `AwsSolutions-IAM5` wildcards, scope with `applies_to=["Resource::*"]` or a specific pattern and explain *why* the wildcard is unavoidable.

**Critical local-vs-CI gap**: `make test-cdk` uses `Template.from_stack()`, which synthesizes the stack but does **NOT** raise on cdk-nag Aspect errors. The CI `cdk-check` job runs `cdk synth '**'` via the CLI which does. Passing local tests is necessary but not sufficient — a clean local run can still ship a cdk-nag-failing commit. Either start Docker and run `make cdk-synth` locally before pushing IAM-touching code, or expect to iterate against CI.

## Encryption posture

Every data-bearing resource that supports a per-resource customer-managed key uses the project's CMK: DynamoDB, Lambda env vars, all log groups, the frontend S3 bucket, AppConfig hosted configuration content, SQS DLQs, and CloudTrail trail log files (per-object SSE-KMS into an SSE-S3 bucket). Account/region-wide encryption settings (X-Ray, Glue Data Catalog) are deliberately out of scope — they'd mutate state shared with other apps in the account.

Service-principal grants on CMKs must be confused-deputy-guarded with `aws:SourceAccount` + `aws:SourceArn`. See `grant_logs_service_to_key` / `grant_guardduty_service_to_key` in `hello_world/nag_utils.py` for the canonical pattern.

## Dangling-resource cleanup pattern

Services that create supporting resources outside CloudFormation (CloudWatch log groups, dashboards) don't get cleaned up by `cdk destroy`. Two cleanup `AwsCustomResource` patterns ship in this repo:

- `AppInsightsDashboardCleanup` — deletes the auto-created Application Insights dashboard
- `RumLogGroupCleanup` — deletes the auto-created `/aws/vendedlogs/RUMService_*` log group

When adding services that create supporting AWS resources outside CFN, mirror the pattern: Lambda-backed `cr.AwsCustomResource` with an `on_delete` SDK call, IAM scoped to the specific resource ARN, `ignore_error_codes_matching="ResourceNotFoundException"` for the case where the resource never materialized.

## Conventional Commits + git-cliff drive `CHANGELOG.md`

Commit prefix grammar (see README "Commit message convention"):

`feat:` / `fix:` / `docs:` / `chore:` / `ci:` / `test:` / `refactor:` / `build:`

`cliff.toml` maps these to Keep-a-Changelog groups. Regenerate with `git cliff -o CHANGELOG.md`. Dependabot bumps and `Merge pull request` commits are filtered out by design. Release recipe in README "Cutting a release" — driven by `git cliff --bumped-version`, `make lock`, annotated tag, `gh release create --notes-from-tag --latest --verify-tag`.

## Behaviors to avoid

- **No `Co-Authored-By:` trailer on commits.** Personal preference.
- **Don't introduce account/region-wide CDK constructs** without flagging them explicitly. `glue.CfnDataCatalogEncryptionSettings`, `xray.UpdateEncryptionConfig`, and similar mutate state shared with other apps in the deploying account. Forks dropping this stack into an existing AWS account would silently override neighbor teams' settings.
- **Don't commit `cdk.out/`, `report.html`, `htmlcov/`, `.coverage`, or `site/`.** Reproducible from source; gitignored already.

## Forking this template

When a fork is spawned from this template via GitHub's "Use this template":

1. **Edit this CLAUDE.md's "Project" section** to describe the fork's workload, and add a workload-specific guidance section near the bottom (see [nba-data-api/CLAUDE.md](https://github.com/timpugh/nba-data-api/blob/main/CLAUDE.md) for an example).
2. **Run the post-template setup steps**: enable GitHub Pages (`gh api repos/<owner>/<repo>/pages -X POST -f build_type=workflow` — requires the repo to be public on the free plan), bootstrap CDK in the target account+region (`cdk bootstrap aws://<account>/us-east-1`), and walk through the Production readiness checklist in `TODO.md` before customer traffic.
3. **Don't drift from this template silently.** If you fix something here that other forks would benefit from, push it back upstream. If you change something that diverges intentionally (different encryption posture, different observability stack), document the *why* in your fork's CLAUDE.md so future contributors don't try to reconcile.
