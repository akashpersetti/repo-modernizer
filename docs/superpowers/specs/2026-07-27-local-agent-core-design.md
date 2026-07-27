# RepoModernizer Sub-Project 1: Local Agent Core — Design

**Parent spec:** `RepoModernizer-Spec.md` (Build Order §11, steps 1–3, local half)
**Scope of this sub-project:** the LangGraph migration graph running entirely locally against a filesystem repo. No FastAPI, no AWS infra, no GitHub PR integration. Those are later sub-projects (4: FastAPI+GitHub wrapper, 5–8: Terraform/AWS deploy). DynamoDB checkpointing and crash-recovery `/resume` are also deferred (sub-project 3) — this phase uses LangGraph's in-memory checkpointer.

## Why this scope

The parent spec's own Build Order says steps 1–3 (graph running locally, guardrails/risk/interrupt/budget, crash-recovery) is a stronger standalone portfolio piece than the full cloud build. This sub-project covers the "no AWS yet" portion of that (steps 1–2); DynamoDB-backed resume is a follow-on sub-project once this is solid.

## Decisions locked in for this sub-project

- **LLM provider:** Bedrock primary, second Bedrock model as fallback (both require local AWS credentials; no non-AWS API key needed yet).
- **Demo/test goal:** Flask → FastAPI async, against a fixture repo we author (`fixtures/sample_repo`).
- **Checkpointer:** LangGraph `MemorySaver` (in-process only). Risk-gated interrupts are approved via a terminal `input()` prompt. Swapped for a DynamoDB checkpointer in sub-project 3 to get real crash/restart recovery.
- **Entry point:** a CLI (`app/cli.py`), e.g. `repomod run --repo <path> --goal "..." --test-cmd "pytest -q"`.
- **Python tooling:** `uv`, target Python 3.12.

## Architecture

Single Python package. CLI builds the graph and runs it in-process against a local repo path.

```
repomodernizer/
├── app/
│   ├── cli.py                  # entry point: repomod run --repo --goal --test-cmd
│   ├── agent/
│   │   ├── graph.py            # graph wiring: ingest→plan→migrate_file(loop)→finalize
│   │   ├── nodes.py
│   │   ├── state.py            # TypedDict graph state
│   │   ├── guardrails.py
│   │   ├── risk.py
│   │   ├── budget.py
│   │   └── providers.py        # Bedrock primary + Bedrock fallback model, retry/backoff
│   ├── services/
│   │   ├── diffs.py            # unified diff generation + apply + validation
│   │   └── tests_runner.py     # subprocess pytest execution + pass/fail parse
│   └── config.py               # pydantic-settings
├── fixtures/sample_repo/       # tiny Flask app + pytest suite (migration target)
├── tests/
│   ├── test_guardrails.py test_risk.py test_budget.py
│   └── test_graph_integration.py   # full run against fixtures/sample_repo
├── runs/                       # local output: diffs, trace.json, summary per task_id (gitignored)
├── pyproject.toml
├── .env.example
└── README.md
```

## State schema

```python
class FileResult(TypedDict):
    path: str
    status: Literal["pending","migrated","approved","rejected","failed","skipped"]
    tokens: int
    cost_usd: float
    retry_count: int

class GraphState(TypedDict):
    task_id: str
    repo_path: str
    goal: str
    test_command: str
    plan: list[dict]          # [{path, rationale, risk_score}]
    files: dict[str, FileResult]
    cursor: int                # index into plan, drives the migrate_file loop
    cost_used_usd: float
    provider_used: str
    trace: list[dict]          # append-only step log, written to runs/<task_id>/trace.json
```

## Nodes

- **ingest** — copy `repo_path` into `runs/<task_id>/workspace/`, init a git branch there, seed state.
- **plan** — one LLM call over the tree → ordered file list + rationale + risk_score (0–1) per file. Checkpoint.
- **migrate_file** — loop node, one file per pass (`cursor` advances):
  1. read file + light dependency context (imports)
  2. LLM call → unified diff
  3. `guardrails.validate_diff()` — reject on forbidden path, file delete, oversize hunk, out-of-target-file write
  4. `risk.score()` recheck — if `>= RISK_APPROVAL_THRESHOLD`, `interrupt()`; CLI prints the diff and prompts `approve/reject`
  5. apply diff to the workspace branch
  6. `tests_runner.run()` — subprocess `test_command`; on fail, bounded retry (`MAX_FILE_RETRIES`) feeding stderr back into the next LLM call
  7. on repeated model error → `providers.py` failover (Bedrock model A → Bedrock model B), logged in trace
  8. `budget.py` — accumulate tokens→$; if the projected cost of the *next* file would push `cost_used_usd` over `MAX_TASK_COST_USD`, hard-stop: mark remaining files `skipped`, jump to finalize
  9. checkpoint state
- **finalize** — write `runs/<task_id>/trace.json` + `summary.md` (status table per file, total cost, provider-switch log) and `runs/<task_id>/diffs/` per-file diff bundle. No PR yet — that's sub-project 4.

## Guardrails, risk, budget

- **`guardrails.py`** — pure functions, no LLM/IO. Reject a diff if: it deletes a file; touches a path matching `FORBIDDEN_PATHS` (`.github/`, `migrations/`, `*.lock`); writes outside the targeted file; changed-line count exceeds a configured cap (e.g. 400). Signature: `validate_diff(diff: str, target_path: str) -> tuple[bool, str | None]`.
- **`risk.py`** — heuristic 0–1 score from: normalized lines-changed, presence of auth/db/security-sensitive tokens (`password`, `secret`, `token`, `session`, `sql`, `auth`) in the diff, whether a test file covers the target path. Weighted sum, clamped to [0,1]. Pure/unit-testable.
- **`budget.py`** — tracks cumulative `(tokens_in, tokens_out) → dollars` per task via a per-model pricing table. Exposes `would_exceed(next_estimate) -> bool`, checked *before* each file starts (not only after), so one expensive file can't blow past the cap.
- **`providers.py`** — `generate(prompt) -> (text, tokens_in, tokens_out, provider_name)`. Wraps the primary Bedrock model with bounded retry+backoff (e.g. 3 attempts, exponential); on final failure, calls the fallback Bedrock model and logs the switch into `state.trace`.

## Testing

- `test_guardrails.py`, `test_risk.py`, `test_budget.py` — pure unit tests, table-driven, no LLM calls, fast and always-on.
- `test_graph_integration.py` — runs the full graph against `fixtures/sample_repo` (small Flask app, ~4–5 files + pytest suite) with goal "Flask to FastAPI async"; asserts every file ends `migrated`/`approved` and the migrated workspace's own test suite passes. This test calls Bedrock — gate it behind an env flag so plain `pytest -q` stays fast and free of live model calls.

## Out of scope (deferred to later sub-projects)

- DynamoDB checkpointer, `/resume` after process crash (sub-project 3)
- FastAPI endpoints, SQS worker split (sub-project 4)
- GitHub clone/branch/PR integration (sub-project 4)
- Terraform, Lambda, Fargate, zero-standing-cost infra (sub-projects 5–8)
- Dashboard UI
