# RepoModernizer — Local Agent Core

Sub-project 1 of RepoModernizer: a LangGraph-driven agent that migrates a repo file-by-file
toward a stated goal, gated by guardrails, risk scoring, and a cost cap, with human-in-the-loop
approval on risky diffs. Runs entirely locally — no AWS infra beyond Bedrock inference calls.

Full project spec: [`RepoModernizer-Spec.md`](../RepoModernizer-Spec.md).
This sub-project's design: [`docs/superpowers/specs/2026-07-27-local-agent-core-design.md`](docs/superpowers/specs/2026-07-27-local-agent-core-design.md).

## Setup

```bash
uv sync
cp .env.example .env   # fill in AWS credentials with Bedrock access
```

## Run a migration

```bash
uv run repomod run --repo ./fixtures/sample_repo --goal "Flask to FastAPI async" --test-cmd "pytest -q"
```

Output (trace, summary, per-file diffs) is written to `runs/<task_id>/`.

## Run the service (sub-project 2)

```bash
DDB_TABLE_CHECKPOINTS=repomod-checkpoints ./scripts/create_checkpoints_table.sh   # once
uv run uvicorn app.main:app --reload
```

```bash
curl -s -X POST localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{"repo_url":"https://github.com/<you>/repomodernizer-demo-target",
       "goal":"Flask to FastAPI async","test_command":"pytest -q"}' | jq

curl -s localhost:8000/tasks/<task_id> | jq

# if awaiting_approval is non-null:
curl -s -X POST localhost:8000/tasks/<task_id>/approve \
  -H 'content-type: application/json' \
  -d '{"file":"webapp.py","decision":"approve"}' | jq
```

**Crash-recovery demo:** start a task, kill the `uvicorn` process mid-run (`Ctrl+C`), restart it, then:
```bash
curl -s -X POST localhost:8000/tasks/<task_id>/resume | jq
```
It continues from the last DynamoDB checkpoint rather than restarting. See `tests/test_crash_recovery.py` for the automated version of this proof.

## Tests

```bash
uv run pytest -q                              # fast unit tests, no network
RUN_LIVE_BEDROCK_TESTS=1 uv run pytest -q -s  # + live end-to-end migration against fixtures/sample_repo
RUN_LIVE_GITHUB_TESTS=1 DEMO_REPO_URL=https://github.com/<you>/repomodernizer-demo-target uv run pytest tests/test_github_live.py -v -s  # real Bedrock + real GitHub PR (costs money)
```
