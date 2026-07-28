# RepoModernizer Sub-Project 3a: Infra + Deploy — Design

**Parent spec:** `RepoModernizer-Spec.md` (Build Order §11, steps 5–6, 8)
**Prior sub-projects:** local agent core (sub-project 1), durable service layer (sub-project 2) — both implemented, live-tested, pushed to `main`.
**Scope of this sub-project:** deploy the service to AWS for real — Terraform infra, the Lambda(API)/Fargate(worker) split via SQS, GitHub Actions OIDC CI/CD. Dashboard, demo GIF, and README polish are sub-project 3b (Next.js/Tailwind/App Router, per the user's stated preference — brainstormed separately when we get there).

## Why this scope

Spec's Build Order groups infra + the Lambda/Fargate split + CI/CD as one deploy phase (steps 5–6), with the $0-idle verification (step 8) as its natural conclusion. This is the sub-project that actually makes the durable service (sub-project 2) a deployed, production artifact rather than something that only runs on a laptop.

## Decisions locked in for this sub-project

- **Terraform state:** S3 backend with DynamoDB state locking (`repomod-tf-lock`) — the one deliberate exception to "no extra infra," purely for Terraform's own bookkeeping.
- **Lambda packaging:** container image for the API Lambda (Mangum-wrapped FastAPI), matching the amd64 discipline already required for the Fargate worker (spec §8c) and sidestepping native-wheel platform mismatches (`pydantic-core`, `boto3`). The SQS consumer Lambda stays a plain zip — it's a few lines of boto3 glue with no third-party deps, a second container image would be pure overhead.
- **Worker trigger:** SQS → Lambda consumer (event source mapping) → `ecs:RunTask`, one task per message. No `aws_ecs_service`, no autoscaling policy — matches spec §9b's fargate.tf exactly, task count is 0 whenever nothing is running.
- **CI/CD depth:** a real, working GitHub Actions OIDC pipeline — actual `.github/workflows/deploy.yml`, actual IAM role via `github_oidc.tf`, actual repo secrets on `akashpersetti/repo-modernizer`, verified with a real merge. No lint/typecheck in CI (ruff/mypy aren't configured on this codebase yet; retrofitting them across 2 prior sub-projects' code is separate, explicit scope if wanted later — not silently bundled here).
- **Worker re-architecture (the one real code change):** sub-project 2's `TaskRunner` ran migrations via an in-process background thread inside one long-lived FastAPI process. That model doesn't fit Lambda (short-lived) or a one-shot Fargate task (no persistent process). `POST /tasks` (Lambda) becomes enqueue-only: validate, `SQS.send_message`, return `task_id` immediately. The actual clone+graph-invoke work moves to a new `app/worker/entrypoint.py` that Fargate runs once per SQS message — for `start`, `approve`, and `resume` alike — then exits.
- **Workspace persistence across separate one-shot tasks (correction found while designing `entrypoint.py`):** each Fargate task is a fresh container — the "start" action's cloned git workspace lives only in that container's ephemeral storage. A later `approve`/`resume` action runs in a **separate** container/task, which has no access to that workspace, even though the LangGraph checkpoint (file statuses, cursor) does persist in DynamoDB. Fixed with an EFS volume (`infra/efs.tf`) mounted at `/mnt/workspace` in the task definition, one subdirectory per `task_id`, so the on-disk git working tree survives across separate task invocations for the same task. Bills per-GB-stored with no idle compute cost, consistent with the zero-standing-cost philosophy. `repo_url`, `branch`, and `base_branch` also move into `GraphState` itself (new fields, checkpointed like everything else) so a fresh container handling `approve`/`resume` can reconstruct the PR-finalize step from `graph.invoke()`'s returned state alone, with no separate in-memory context needed.

## Architecture

```
Client → API Gateway (HTTP API) → Lambda (FastAPI + Mangum, container image)
                                     │
                    POST /tasks: validate → SQS.send_message → return task_id
                    GET /tasks/{id}: read-only graph.get_state() via DynamoDBCheckpointer
                    POST /approve, /resume: validate → SQS.send_message → 202
                                     │
                                     ▼
                              SQS queue (repomod-tasks)
                                     │
                    Lambda consumer (SQS event source mapping)
                                     │  ecs:RunTask, one task per message,
                                     │  message body passed as container env overrides
                                     ▼
                         ECS Fargate task (one-shot, no standing service)
                    app/worker/entrypoint.py:
                      - action=start:   clone repo, build graph, graph.invoke(initial_state)
                      - action=approve: build graph (reconnect via checkpointer+task_id),
                                        graph.invoke(Command(resume={...}))
                      - action=resume:  build graph, graph.invoke(None)
                      - if graph reaches END (not interrupted): commit/push/open PR
                      - task exits either way — no polling loop, no persistent process
```

Both Lambda (API) and the Fargate worker package as container images, amd64, per spec §8c.

## SQS message contract

One shape for all three actions, sent by the API, consumed by `entrypoint.py`:
```json
{"action": "start|approve|resume", "task_id": "...", "repo_url": "...", "goal": "...",
 "test_command": "...", "base_branch": "...", "file": "...", "decision": "...", "note": "..."}
```
`task_id` is generated by the API for `start` (uuid) and reused as-is for `approve`/`resume`.

## Terraform infra

New/changed files beyond the original spec's list:
- `infra/backend.tf` — S3 backend (state) + `repomod-tf-lock` DynamoDB table, on-demand billing.
- `infra/vpc.tf` — spec §9b exactly: public subnet, IGW route (no NAT), free gateway endpoints for S3+DynamoDB.
- `infra/dynamodb.tf` — the `repomod-checkpoints` table definition. Already exists (created by hand in sub-project 2) — requires a one-time `terraform import aws_dynamodb_table.checkpoints repomod-checkpoints` rather than a fresh create, to keep existing checkpoint data.
- `infra/sqs.tf` — `repomod-tasks` standard queue, visibility timeout 900s (worker's max runtime + buffer).
- `infra/ecr.tf` — two repos: `repomod-api`, `repomod-worker`, keep-last-3 lifecycle policy on both.
- `infra/efs.tf` — EFS filesystem + mount target in the public subnet + access point, for the persistent per-`task_id` workspace across separate Fargate task invocations (see correction above).
- `infra/lambda.tf` — API Lambda (container image) + consumer Lambda (zip).
- `infra/fargate.tf` — cluster + task definition only, no service (per spec §9b). Task definition adds an EFS-backed volume mounted at `/mnt/workspace`.
- `infra/apigateway.tf` — HTTP API, routes matching spec §6, Lambda proxy integration.
- `infra/iam.tf` — API Lambda role: DynamoDB + Bedrock + `sqs:SendMessage`. Consumer Lambda role: `ecs:RunTask` + `iam:PassRole`. Fargate task role: DynamoDB + Bedrock (GitHub auth is just the PAT in env, no IAM needed).
- `infra/github_oidc.tf` — OIDC provider + deploy role, trust policy scoped to `akashpersetti/repo-modernizer`.
- `infra/budgets.tf`, `infra/logs.tf` — spec §8b: $5/$10 tripwires, 7-day log retention everywhere.

## App code changes

- `app/api/routes_tasks.py` — rewritten to be enqueue-only for `POST /tasks`/`approve`/`resume`; `GET /tasks/{id}` stays a read-only `graph.get_state()` call (dummy `NodeDeps` — a status read never invokes a node, so no real Bedrock/GitHub creds needed for this path).
- `app/worker/entrypoint.py` (new) — the Fargate container's main process. Reads `action`/`task_id`/etc. from environment variables (populated by the consumer Lambda's `RunTask` container overrides), builds the real `NodeDeps`/graph/checkpointer, dispatches on `action`, runs the same commit/push/PR finalize logic sub-project 2's `TaskRunner._run_and_maybe_finalize` had (ported over, since `TaskRunner`'s threading wrapper itself goes away in favor of this one-shot script). The finalize step reads `repo_path`/`repo_url`/`branch`/`base_branch`/`goal` straight off `graph.invoke()`'s returned state — no separate context object needed, since those fields are now part of `GraphState` and checkpointed.
- `app/agent/state.py` — `GraphState` gains `repo_url: str`, `branch: str`, `base_branch: str` fields, seeded once at `start` and left untouched by every existing node (guardrails/risk/budget logic is unaffected) — purely so a fresh container handling `approve`/`resume` has everything it needs from the checkpoint alone.
- `app/worker/consumer_handler.py` (new) — the SQS-triggered Lambda's handler: reads the SQS event, calls `ecs.run_task` with the message body as container environment overrides.
- `TaskRunner`/`app/worker/runner.py` from sub-project 2 is retired — its logic is absorbed into `entrypoint.py` (the graph-invoke + finalize parts) and `routes_tasks.py` (the enqueue parts). Sub-project 2's local CLI-adjacent tests (`test_runner.py`) get replaced by tests against `entrypoint.py`'s three action branches.

## Testing / verification

Mostly real deployment verification rather than added unit-test surface:
- `terraform plan`/`apply` run for real against the existing AWS account.
- `entrypoint.py`'s three action branches (`start`/`approve`/`resume`) unit-tested with a fake SQS-shaped message + real graph/checkpointer (same fake-provider pattern as sub-project 2's tests), mocking only what's genuinely external.
- One real end-to-end migration through the **deployed** stack: real API Gateway URL → SQS → consumer Lambda → `RunTask` → Fargate worker → real DynamoDB checkpoint → real GitHub PR. Not the local sub-project-2 path — this is the actual production path.
- CI/CD verified with a real merge to `main`: images build+push, Terraform applies, Lambda/task-def update.
- $0-idle check (spec §8b step 8) is a concrete follow-up, not something verified same-session: deploy, run once, note the timestamp, check AWS Cost Explorer (filtered by tag `project = repomodernizer`) after 48h reads $0.00 for idle periods.

**Realistic expectation:** this is the largest, most infra-heavy sub-project yet — many more moving AWS pieces than sub-project 2's live run (which surfaced 4 real bugs invisible to unit tests: a race condition, a credential-doubling bug, a branch-name mismatch, a bytecode-pollution bug). Expect comparable or more iteration here: IAM permission gaps, image-digest wiring, Lambda cold-start/env-var issues are the likely categories.

## Out of scope (deferred to sub-project 3b)

- Dashboard UI (Next.js, Tailwind, App Router)
- Demo GIF
- Final README polish (architecture diagram embed, "run it yourself" section, recruiter-skim-optimized first screen)
- Lint/typecheck CI (ruff, mypy) — explicit future scope if wanted, not bundled here
