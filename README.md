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

## Tests

```bash
uv run pytest -q                              # fast unit tests, no network
RUN_LIVE_BEDROCK_TESTS=1 uv run pytest -q -s  # + live end-to-end migration against fixtures/sample_repo
```
