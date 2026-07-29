# RepoModernizer

Autonomous repository modernization agent. Give it a GitHub repo and a goal ("Flask → FastAPI"),
and it plans a file-by-file migration, rewrites each file, runs the target repo's own test suite
after every change, pauses for human approval on risky diffs, survives crashes mid-migration,
fails over across model providers, enforces a hard cost cap, and opens a real pull request —
end to end, deployed on AWS.

**Live dashboard:** https://repo-modernizer.akashpersetti.com
**Live API:** https://6yncgq73gk.execute-api.us-east-1.amazonaws.com

## What this proves

| Scarce signal | How this project proves it |
|---|---|
| Autonomous execution (not chat) | The agent runs an unattended multi-step migration to completion |
| Durability / crash-recovery | LangGraph checkpointer → DynamoDB; a fresh Fargate task resumes from the last committed step, verified with a real kill-mid-run test (`tests/test_crash_recovery.py`) |
| Human-in-the-loop | `interrupt()` before applying high-risk diffs; approve/reject via the dashboard or the API |
| Fault tolerance | Bounded retries with backoff + Bedrock model failover, verified live |
| Cost governance | Per-task token+dollar budget with a hard stop |
| Guardrails | Path allowlist, no destructive ops, diff validation, forbidden-file list |
| Production deploy | API Gateway + Lambda, SQS-triggered Fargate worker with an EFS-backed workspace, Terraform, GitHub Actions OIDC — no long-lived AWS keys anywhere |
| Zero standing cost | No NAT Gateway, no ALB, no EC2, everything scales to zero when idle |

## Architecture

```
Browser (S3 + CloudFront, static Next.js export)
   │ client-side fetch, CORS
   ▼
API Gateway → Lambda (FastAPI + Mangum) — validates, enqueues to SQS, reads DynamoDB for status
                    │
                    ▼
              SQS (repomod-tasks)
                    │
        Lambda consumer → ecs:RunTask (one task per message)
                    │
                    ▼
        Fargate worker (one-shot, EFS-mounted workspace)
        clone → LangGraph agent (plan → migrate_file loop → finalize) → PR
                    │
        DynamoDB (checkpoints, survives across separate task runs)
```

## Run it yourself

The live dashboard above is the easiest way. To run locally instead:

```bash
uv sync
cp .env.example .env   # fill in AWS credentials with Bedrock access
uv run repomod run --repo ./fixtures/sample_repo --goal "Flask to FastAPI async" --test-cmd "pytest -q"
```

Or run the full local service (sub-project 2's path, no AWS infra beyond Bedrock+DynamoDB):

```bash
DDB_TABLE_CHECKPOINTS=repomod-checkpoints ./scripts/create_checkpoints_table.sh   # once
uv run uvicorn app.main:app --reload
```

```bash
curl -s -X POST localhost:8000/tasks -H 'content-type: application/json' \
  -d '{"repo_url":"https://github.com/<you>/repomodernizer-demo-target","goal":"Flask to FastAPI async","test_command":"pytest -q"}' | jq

curl -s localhost:8000/tasks/<task_id> | jq
```

**Crash-recovery demo:** start a task, kill the process mid-run, restart, then `POST /tasks/<task_id>/resume` — it continues from the last checkpoint rather than restarting. See `tests/test_crash_recovery.py` for the automated proof.

## Tests

```bash
.venv/bin/python -m pytest -q                                    # fast suite, no network
RUN_LIVE_BEDROCK_TESTS=1 .venv/bin/python -m pytest -q -s         # + live migration against fixtures/sample_repo
RUN_LIVE_GITHUB_TESTS=1 DEMO_REPO_URL=<url> .venv/bin/python -m pytest tests/test_github_live.py -v -s
```

## Frontend dev

```bash
cd frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev   # http://localhost:3000, talks to a local backend (see "Run the full local service" above) —
                                                          # the deployed API's CORS allow_origins only includes the CloudFront domain, so
                                                          # localhost:3000 can't call it cross-origin
```

## Cost

Deliberately no NAT Gateway, no ALB, no EC2, no standing Fargate service — everything scales to
zero. AWS Budgets tripwires at $5/$10. See `RepoModernizer-Spec.md` §8b for the full zero-idle-cost
design.
