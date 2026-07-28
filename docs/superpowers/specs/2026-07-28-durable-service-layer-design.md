# RepoModernizer Sub-Project 2: Durable Service Layer — Design

**Parent spec:** `RepoModernizer-Spec.md` (Build Order §11, steps 3–4)
**Prior sub-project:** `docs/superpowers/specs/2026-07-27-local-agent-core-design.md` (local LangGraph agent, no AWS beyond Bedrock — implemented, tested against real Bedrock, pushed to `main`)
**Scope of this sub-project:** turn the local CLI-driven agent into a durable, crash-recoverable, API-driven service that opens real GitHub PRs. DynamoDB checkpointer replaces `MemorySaver`. FastAPI replaces the CLI as the driver. GitHub clone/branch/commit/PR replaces local-path input and local-only output. No SQS/Fargate split, no Terraform, no dashboard — those are sub-project 3 (Terraform infra + deploy + polish).

## Why this scope

Spec's own Build Order groups "DynamoDB checkpointer + `/resume`" (step 3, the crash-recovery demo — "your interview money shot") and "wrap in FastAPI; add GitHub clone + PR" (step 4) together as the layer that turns the local agent into the actual production interface, before any cloud infrastructure exists to deploy it to. This sub-project delivers exactly that: everything runs on your laptop, but it's the *real* service — real DynamoDB, real GitHub PRs, real crash recovery — with cloud deployment deferred to sub-project 3.

## Decisions locked in for this sub-project

- **DynamoDB target:** a real on-demand AWS DynamoDB table (not LocalStack) — spec §9 already calls for testing against real AWS dev resources, and on-demand billing means no idle cost.
- **Table creation:** a one-off `scripts/create_checkpoints_table.sh` (`aws dynamodb create-table`), not Terraform yet — replaced by `infra/dynamodb.tf` in sub-project 3.
- **Checkpointer implementation:** a custom `BaseCheckpointSaver` subclass matching spec §7's schema exactly (PK `TASK#{task_id}`, SK `CKPT#{checkpoint_id}`, 14-day TTL) — not a generic community package, which would impose its own schema.
- **Task registry:** skipped. Spec §7 also defines a separate lightweight "Tasks" table for a future "list all running tasks" view, but no such endpoint is in scope here (only `GET /tasks/{id}` for one task). Status is derived directly from the latest checkpoint's `GraphState` via `graph.get_state(config)`. Revisit only if a list-view endpoint is actually needed later.
- **Task execution model:** FastAPI `POST /tasks` spawns a real `threading.Thread` running `graph.invoke(...)` synchronously (not `asyncio`'s native background tasks, since every graph node is blocking sync code) — keeps the API's event loop free to serve concurrent `/health` and `/tasks/{id}` requests. No SQS/Fargate split yet; that's purely an infra concern deferred to sub-project 3, requiring no app code changes.
- **GitHub test target:** a new throwaway public GitHub repo under the user's account, seeded with `fixtures/sample_repo`'s content — safe to open real branches/PRs against during dev and the live integration test.

## Architecture

```
app/
├── main.py                      # FastAPI app instance
├── api/
│   ├── routes_tasks.py          # POST/GET /tasks, /approve, /resume
│   └── routes_health.py         # GET /health
├── agent/
│   ├── checkpointer.py          # DynamoDBCheckpointer(BaseCheckpointSaver)
│   └── ... (existing: graph.py, nodes.py, state.py, guardrails.py, risk.py, budget.py, providers.py)
├── services/
│   ├── github.py                # clone, branch, commit, push, open PR
│   └── ... (existing: diffs.py, tests_runner.py)
├── worker/
│   └── runner.py                 # TaskRunner: background thread per task, status derivation, GitHub finalize step
└── config.py                     # + github_app_token, github_default_base_branch, ddb_table_checkpoints
scripts/
└── create_checkpoints_table.sh   # one-off: aws dynamodb create-table, on-demand billing
tests/
├── test_checkpointer.py          # round-trip against the real DynamoDB table
├── test_github.py                # clone/branch/commit unit-tested against a local bare git repo
├── test_github_live.py           # gated (RUN_LIVE_GITHUB_TESTS=1): real clone/branch/push/PR
├── test_routes.py                # FastAPI TestClient, TaskRunner/graph mocked
└── test_crash_recovery.py        # the money-shot demo as an automated test
```

## Checkpointer

`app/agent/checkpointer.py` — `DynamoDBCheckpointer(BaseCheckpointSaver)`, backed by table `repomod-checkpoints`. PK `TASK#{task_id}` (task_id == LangGraph `thread_id`), SK `CKPT#{checkpoint_id}` — LangGraph's checkpoint IDs are lexicographically sortable, so SK ordering alone gives correct history without a separate sequence counter. TTL attribute set 14 days out per spec §7. Serializes checkpoint/metadata objects with LangGraph's own `JsonPlusSerializer` before storing as a DynamoDB string attribute; deserializes symmetrically on read.

Implements the sync methods LangGraph's `BaseCheckpointSaver` contract requires: `put`, `put_writes`, `get_tuple`, `list`.

**Flag** (same caveat as sub-project 1's interrupt API): `BaseCheckpointSaver`'s exact method signatures can shift between LangGraph versions. Verify against the actually-installed version's base class before writing the implementation — TDD surfaces any mismatch immediately as an import/signature error, not a silent bug, the same way sub-project 1's interrupt/resume API was verified by running the tests rather than trusting the plan's assumed signature.

## GitHub integration

`app/services/github.py` — plain functions, no framework wrapping:

```python
def clone_repo(repo_url: str, dest: Path, token: str) -> None
def create_branch(repo_path: Path, branch_name: str) -> None
def commit_all(repo_path: Path, message: str) -> None
def push_branch(repo_path: Path, branch_name: str, token: str) -> None
def open_pull_request(repo_url: str, branch: str, base: str, title: str, body: str, token: str) -> str  # returns PR URL
```

`open_pull_request` calls GitHub's REST API directly via `httpx` (already a dependency from sub-project 1's fixture test setup) — no separate GitHub SDK.

## Task execution and completion flow

Core agent graph (`nodes.py`) is untouched — no GitHub awareness threaded into `NodeDeps`. `TaskRunner` (`app/worker/runner.py`) owns the GitHub step, sitting outside the LangGraph state machine entirely:

```python
def _run_and_maybe_finalize(graph, invoke_arg, config, repo_ctx) -> None:
    result = graph.invoke(invoke_arg, config=config)
    if "__interrupt__" in result:
        return  # paused, waiting for /approve — thread just ends here
    if any(f["status"] in ("migrated", "approved") for f in result["files"].values()):
        github.push_branch(repo_ctx.path, repo_ctx.branch, repo_ctx.token)
        github.open_pull_request(
            repo_ctx.repo_url, repo_ctx.branch, repo_ctx.base_branch,
            title=f"RepoModernizer: {repo_ctx.goal}", body=..., token=repo_ctx.token,
        )
    # else: nothing migrated, nothing to push, no empty PR
```

`TaskRunner` methods, all funneling through this helper:
- `start(task_id, repo_url, goal, test_command)` — clones the repo, spawns a background thread running `_run_and_maybe_finalize` with the initial state.
- `get_status(task_id)` — uses `graph.get_state(config)` against the same compiled graph + `DynamoDBCheckpointer` to read the current `GraphState` (files/status/cost) plus `.next`/`.tasks[*].interrupts` to detect "awaiting approval" and surface the pending diff for display.
- `approve(task_id, file, decision, note)` — spawns a background thread running `_run_and_maybe_finalize` with `Command(resume={"decision": decision, "note": note})`.
- `resume(task_id)` — same mechanism, triggered externally after a crash instead of after an interrupt: `_run_and_maybe_finalize` with `invoke_arg=None`, which continues from the last DynamoDB checkpoint since no in-memory state is needed for LangGraph to pick back up.

## API routes

`app/api/routes_tasks.py` — thin Pydantic-validated wrappers over `TaskRunner`, matching spec §6:
- `POST /tasks` — `{repo_url, goal, test_command, options}` → clones repo (4xx immediately on clone/auth failure, no `task_id` issued on failure) → `task_id`.
- `GET /tasks/{id}` — status + file matrix + cost, derived live from the checkpoint.
- `POST /tasks/{id}/approve` — `{file, decision, note}` → resumes.
- `POST /tasks/{id}/resume` — resumes after a crash or a caught exception.
- `GET /health` — liveness.

`GET /tasks/{id}/trace` is deferred — no S3 in this sub-project. `finalize_node` keeps writing local `trace.json`/`summary.md` (carried over from sub-project 1) as a debugging fallback even though the API is now the primary interface.

## Error handling

- Bad `repo_url` / clone auth failure → `POST /tasks` returns 4xx immediately, no `task_id`, no background thread starts.
- Uncaught exception mid-run (e.g. DynamoDB throttling exhausted beyond provider retries) → caught at the top of the thread body, recorded against `task_id` in a small in-memory `{task_id: last_error}` map (no registry table, see above). State is already checkpointed up to the last completed step. `POST /tasks/{id}/resume` is exactly this recovery path — same mechanism whether the trigger is a genuine process crash or a caught exception inside a still-running process.
- PR push/open failure → caught the same way, recorded against `task_id`; local `trace.json`/`summary.md` still written for debugging.

## Testing

- `test_checkpointer.py` — round-trip `put`/`get_tuple`/`list` against the real `repomod-checkpoints` table, each test using its own throwaway `task_id` (uuid) to avoid collisions.
- `test_github.py` — `clone_repo`/`create_branch`/`commit_all` unit-tested against a local bare git repo (`file://` remote, no network). `push_branch`/`open_pull_request` can't be faked this way — covered only by the live test below.
- `test_github_live.py` — gated behind `RUN_LIVE_GITHUB_TESTS=1` (same pattern as sub-project 1's `RUN_LIVE_BEDROCK_TESTS`): real clone/branch/commit/push/PR against the new throwaway GitHub repo, asserts a real PR URL comes back.
- `test_routes.py` — FastAPI `TestClient`, `TaskRunner`/graph mocked — no real Bedrock/DynamoDB/GitHub calls, verifies the HTTP contract (status codes, response shapes, approve resumes).
- `test_crash_recovery.py` — the "money shot" as an automated test, not just a manual demo: start a task, monkeypatch a node to raise after N files to simulate a mid-run crash, assert the in-memory thread dies; then call `resume()` against a **fresh** `TaskRunner`/graph instance (new checkpointer connection, simulating a restarted process) and assert it continues from the last completed file rather than re-running from scratch.

## Out of scope (deferred to sub-project 3)

- SQS worker split, Fargate/Lambda deploy, Terraform for all of it
- CI/CD (`.github/workflows/deploy.yml`)
- Dashboard UI, demo GIF, README polish
- S3 artifact storage, `GET /tasks/{id}/trace`
- Separate Tasks registry table / "list running tasks" view
- Zero-standing-cost verification (§8b) — meaningless until there's infra deployed to verify
