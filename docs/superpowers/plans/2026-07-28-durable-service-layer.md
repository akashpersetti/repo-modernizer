# Durable Service Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the local CLI-driven agent (sub-project 1) into a durable, crash-recoverable, API-driven service that opens real GitHub PRs — DynamoDB checkpointer replaces `MemorySaver`, FastAPI replaces the CLI, real GitHub clone/branch/commit/PR replaces local-path input/output.

**Architecture:** `TaskRunner` wraps the existing LangGraph agent core (untouched except two small, generically-justified fixes) with a `DynamoDBCheckpointer`, running each task in a background thread so FastAPI's event loop stays free. GitHub push/PR happens as a step *after* the graph reaches its end state, outside the state machine itself. `/approve` and `/resume` both just re-invoke the same compiled graph against the same `thread_id` — LangGraph's checkpointer handles replay whether the pause was an interrupt or a crash.

**Tech Stack:** FastAPI, `boto3` (DynamoDB + Bedrock), `httpx` (GitHub REST API), threading (no async agent code — nodes are all sync/blocking).

## Global Constraints

- DynamoDB checkpoints table: `repomod-checkpoints`, on-demand billing, TTL 14 days on a `ttl` attribute (spec §7).
- No SQS, no Fargate, no Terraform, no dashboard, no S3 in this sub-project — those are sub-project 3.
- No separate Tasks registry table — status is derived live from the latest checkpoint via `graph.get_state()`.
- `nodes.py`'s core migration logic (guardrails/risk/budget/interrupt flow) is untouched. The only two edits to existing sub-project 1 code are: (1) `build_graph()` accepts an injectable checkpointer, defaulting to `MemorySaver()` so all sub-project 1 tests keep passing unmodified, and (2) `ingest_node`'s baseline commit gets `--allow-empty` so it doesn't crash on an already-clean freshly-cloned repo. Both are generic robustness fixes, not GitHub-specific.
- `GITHUB_APP_TOKEN`, `GITHUB_DEFAULT_BASE_BRANCH` already defined in `.env.example` from the parent spec — add matching `Settings` fields.

---

### Task 1: Config, dependencies, and the checkpoints table

**Files:**
- Modify: `pyproject.toml`
- Modify: `app/config.py`
- Modify: `.env.example`
- Create: `scripts/create_checkpoints_table.sh`
- Test: `tests/test_config.py` (extend)

**Interfaces:**
- Produces: `Settings.github_app_token: str`, `Settings.github_default_base_branch: str`, `Settings.ddb_table_checkpoints: str`.

- [x] **Step 1: Write the failing test**

```python
# tests/test_config.py — add these
def test_settings_has_github_and_checkpoint_defaults(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_TOKEN", raising=False)
    settings = Settings(_env_file=None)
    assert settings.github_default_base_branch == "main"
    assert settings.ddb_table_checkpoints == "repomod-checkpoints"
    assert settings.github_app_token == ""
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError` — fields don't exist yet.

- [x] **Step 3: Update pyproject.toml, config.py, .env.example; write the table-creation script**

```toml
# pyproject.toml — dependencies section becomes:
dependencies = [
    "langgraph>=0.2",
    "boto3>=1.34",
    "pydantic-settings>=2.4",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "httpx>=0.27",
]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "flask>=3.0",
]
```

(`fastapi` and `httpx` move from dev to real dependencies — the service imports them at runtime now, not just tests. `uvicorn` added to actually serve. `flask` stays dev-only, still just for the fixture.)

```python
# app/config.py — add these fields to Settings, right after forbidden_paths / before the method
    github_app_token: str = ""
    github_default_base_branch: str = "main"
    ddb_table_checkpoints: str = "repomod-checkpoints"
```

```
# .env.example — add these lines (spec §5 already lists them, keep in sync)
GITHUB_APP_TOKEN=
GITHUB_DEFAULT_BASE_BRANCH=main
DDB_TABLE_CHECKPOINTS=repomod-checkpoints
```

```bash
#!/usr/bin/env bash
# scripts/create_checkpoints_table.sh
set -euo pipefail

TABLE_NAME="${DDB_TABLE_CHECKPOINTS:-repomod-checkpoints}"
REGION="${AWS_REGION:-us-east-1}"

aws dynamodb create-table \
  --table-name "$TABLE_NAME" \
  --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
  --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION"

aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$REGION"

aws dynamodb update-time-to-live \
  --table-name "$TABLE_NAME" \
  --time-to-live-specification "Enabled=true,AttributeName=ttl" \
  --region "$REGION"

echo "Table $TABLE_NAME created with TTL on 'ttl' attribute."
```

Run `chmod +x scripts/create_checkpoints_table.sh`, then run it once by hand:
`AWS_REGION=us-east-1 DDB_TABLE_CHECKPOINTS=repomod-checkpoints ./scripts/create_checkpoints_table.sh`
Verify: `aws dynamodb describe-table --table-name repomod-checkpoints --query 'Table.TableStatus'` prints `"ACTIVE"`. This table must exist before Task 3's tests can pass — they hit it for real.

Run `uv sync` after the pyproject.toml change to install the new/moved dependencies.

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: PASS (all tests, including the new one)

- [x] **Step 5: Commit**

```bash
git add pyproject.toml app/config.py .env.example scripts/create_checkpoints_table.sh uv.lock
git commit -m "feat: config for GitHub/DynamoDB, checkpoints table creation script"
```

---

### Task 2: Graph core adjustments for service use

**Files:**
- Modify: `app/agent/graph.py`
- Modify: `app/agent/nodes.py`
- Test: `tests/test_graph.py` (extend)

**Interfaces:**
- Modifies: `build_graph(deps: NodeDeps, checkpointer=None)` — now accepts an optional checkpointer, defaults to `MemorySaver()` (unchanged default behavior for every existing caller).

- [x] **Step 1: Write the failing tests**

```python
# tests/test_graph.py — add these
import subprocess

from langgraph.checkpoint.memory import MemorySaver


def test_build_graph_accepts_injected_checkpointer():
    fake = FakeProviderRouter([json.dumps([])])
    deps = NodeDeps(
        providers=fake, budget=BudgetTracker(cap_usd=10.0), forbidden_paths=[],
        max_diff_lines=400, risk_threshold=0.6, max_retries=2, estimated_cost_per_file=0.01,
    )
    injected = MemorySaver()

    graph = build_graph(deps, checkpointer=injected)

    assert graph.checkpointer is injected


def test_ingest_node_does_not_crash_on_already_clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "webapp.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@x.com", "-c", "user.name=x", "commit", "-q", "-m", "already committed"],
        cwd=repo, check=True,
    )
    # simulates a freshly-cloned repo: tree is already clean, nothing new to stage

    from app.agent.nodes import ingest_node
    deps = NodeDeps(
        providers=None, budget=BudgetTracker(cap_usd=10.0), forbidden_paths=[],
        max_diff_lines=400, risk_threshold=0.6, max_retries=2, estimated_cost_per_file=0.01,
    )
    state = {"repo_path": str(repo), "trace": []}

    result = ingest_node(state, deps)  # must not raise

    assert result["trace"][0]["node"] == "ingest"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_graph.py -k "injected_checkpointer or already_clean" -v`
Expected: first FAILs with `TypeError: build_graph() got an unexpected keyword argument 'checkpointer'`; second FAILs with `CalledProcessError` (git commit exits non-zero: "nothing to commit").

- [x] **Step 3: Make the fixes**

```python
# app/agent/graph.py — change the function signature and compile() call
def build_graph(deps: NodeDeps, checkpointer=None):
    graph = StateGraph(GraphState)
    graph.add_node("ingest", partial(ingest_node, deps=deps))
    graph.add_node("plan", partial(plan_node, deps=deps))
    graph.add_node("migrate_file", partial(migrate_file_node, deps=deps))
    graph.add_node("finalize", partial(finalize_node, deps=deps))

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "plan")
    graph.add_conditional_edges("plan", route_after_migrate, {
        "migrate_file": "migrate_file",
        "finalize": "finalize",
    })
    graph.add_conditional_edges("migrate_file", route_after_migrate, {
        "migrate_file": "migrate_file",
        "finalize": "finalize",
    })
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())
```

```python
# app/agent/nodes.py — in ingest_node, add --allow-empty to the commit command
    subprocess.run(
        [
            "git", "-c", "user.email=agent@repomodernizer.local", "-c", "user.name=repomodernizer",
            "commit", "-q", "-m", "baseline", "--allow-empty",
        ],
        cwd=workspace, check=True,
    )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: full suite passes (46+ tests) — the `--allow-empty` and injectable-checkpointer changes must not break any sub-project 1 test.

- [x] **Step 5: Commit**

```bash
git add app/agent/graph.py app/agent/nodes.py tests/test_graph.py
git commit -m "feat: injectable checkpointer in build_graph, allow-empty baseline commit for already-clean repos"
```

---

### Task 3: DynamoDB checkpointer

**Files:**
- Create: `app/agent/checkpointer.py`
- Test: `tests/test_checkpointer.py`

**Interfaces:**
- Produces: `DynamoDBCheckpointer(BaseCheckpointSaver)` — `__init__(table_name: str, resource=None)`; implements `put`, `put_writes`, `get_tuple`, `list` per LangGraph's checkpointer contract.

**Prerequisite:** Task 1's `repomod-checkpoints` table must already exist and be `ACTIVE` — these tests hit it for real (per the earlier decision to test against real AWS, not LocalStack).

- [x] **Step 1: Inspect the actual installed LangGraph checkpointer contract**

Run:
```bash
.venv/bin/python -c "
from langgraph.checkpoint.base import BaseCheckpointSaver
import inspect
for name in ['put', 'put_writes', 'get_tuple', 'list', '__init__']:
    print(name, inspect.signature(getattr(BaseCheckpointSaver, name)))
"
```

The implementation below is written against the standard `put(config, checkpoint, metadata, new_versions)` / `put_writes(config, writes, task_id, task_path="")` / `get_tuple(config)` / `list(config, *, filter=None, before=None, limit=None)` contract. If this command's output shows different parameter names or order for the installed version, adjust the implementation in Step 3 to match — Python will raise `TypeError: Can't instantiate abstract class DynamoDBCheckpointer with abstract method(s) ...` immediately and unambiguously if a required method is missing or misnamed, so a mismatch surfaces at the very first test run, not silently.

- [x] **Step 2: Write the failing tests**

```python
# tests/test_checkpointer.py
import uuid

from app.agent.checkpointer import DynamoDBCheckpointer
from app.config import Settings

_settings = Settings()
_checkpointer = DynamoDBCheckpointer(table_name=_settings.ddb_table_checkpoints)


def _sample_checkpoint(checkpoint_id: str) -> dict:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"cursor": 0, "files": {}},
        "channel_versions": {},
        "versions_seen": {},
    }


def test_put_and_get_tuple_roundtrip():
    thread_id = f"test-{uuid.uuid4().hex[:8]}"
    checkpoint = _sample_checkpoint("ckpt-1")
    config = {"configurable": {"thread_id": thread_id}}

    _checkpointer.put(config, checkpoint, {"step": 0}, {})
    result = _checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})

    assert result is not None
    assert result.checkpoint["id"] == "ckpt-1"
    assert result.checkpoint["channel_values"]["cursor"] == 0


def test_get_tuple_returns_none_for_unknown_thread():
    result = _checkpointer.get_tuple({"configurable": {"thread_id": f"nonexistent-{uuid.uuid4().hex}"}})
    assert result is None


def test_list_returns_newest_first():
    thread_id = f"test-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    _checkpointer.put(config, _sample_checkpoint("ckpt-a"), {"step": 0}, {})
    _checkpointer.put(config, _sample_checkpoint("ckpt-b"), {"step": 1}, {})

    results = list(_checkpointer.list({"configurable": {"thread_id": thread_id}}))

    assert [r.checkpoint["id"] for r in results] == ["ckpt-b", "ckpt-a"]


def test_put_writes_surfaces_as_pending_writes():
    thread_id = f"test-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    _checkpointer.put(config, _sample_checkpoint("ckpt-1"), {"step": 0}, {})

    write_config = {"configurable": {"thread_id": thread_id, "checkpoint_id": "ckpt-1"}}
    _checkpointer.put_writes(write_config, [("files", {"a.py": "pending"})], task_id="task-1")

    result = _checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})

    assert result.pending_writes == [("task-1", "files", {"a.py": "pending"})]
```

- [x] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_checkpointer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent.checkpointer'`

- [x] **Step 4: Write the implementation**

```python
# app/agent/checkpointer.py
import time
from typing import Any, Iterator, Optional

import boto3
from boto3.dynamodb.conditions import Key
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

_TTL_SECONDS = 14 * 24 * 3600


class DynamoDBCheckpointer(BaseCheckpointSaver):
    def __init__(self, table_name: str, resource=None):
        super().__init__()
        self.serde = JsonPlusSerializer()
        self._table = (resource or boto3.resource("dynamodb")).Table(table_name)

    def _put_blob(self, pk: str, sk: str, obj: Any, extra: Optional[dict] = None) -> None:
        type_, blob = self.serde.dumps_typed(obj)
        item = {"PK": pk, "SK": sk, "type": type_, "blob": blob, "ttl": int(time.time()) + _TTL_SECONDS}
        if extra:
            item.update(extra)
        self._table.put_item(Item=item)

    def _load_blob(self, item: dict) -> Any:
        return self.serde.loads_typed((item["type"], bytes(item["blob"])))

    def put(self, config: dict, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions) -> dict:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = checkpoint["id"]
        parent_id = config["configurable"].get("checkpoint_id")
        self._put_blob(
            f"TASK#{thread_id}", f"CKPT#{checkpoint_id}", (checkpoint, metadata),
            extra={"parent_checkpoint_id": parent_id or ""},
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": config["configurable"].get("checkpoint_ns", ""),
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(self, config: dict, writes: list, task_id: str, task_path: str = "") -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"]["checkpoint_id"]
        for idx, (channel, value) in enumerate(writes):
            self._put_blob(f"TASK#{thread_id}", f"WRITE#{checkpoint_id}#{task_id}#{idx}", (task_id, channel, value))

    def _writes_for(self, thread_id: str, checkpoint_id: str) -> list:
        resp = self._table.query(
            KeyConditionExpression=Key("PK").eq(f"TASK#{thread_id}") & Key("SK").begins_with(f"WRITE#{checkpoint_id}#"),
        )
        return [self._load_blob(item) for item in resp.get("Items", [])]

    def _tuple_from_item(self, thread_id: str, item: dict) -> CheckpointTuple:
        checkpoint, metadata = self._load_blob(item)
        found_id = item["SK"].split("#", 1)[1]
        parent_id = item.get("parent_checkpoint_id") or None
        parent_config = {"configurable": {"thread_id": thread_id, "checkpoint_id": parent_id}} if parent_id else None
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id, "checkpoint_id": found_id}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=self._writes_for(thread_id, found_id),
        )

    def get_tuple(self, config: dict) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"].get("checkpoint_id")
        if checkpoint_id:
            resp = self._table.get_item(Key={"PK": f"TASK#{thread_id}", "SK": f"CKPT#{checkpoint_id}"})
            item = resp.get("Item")
        else:
            resp = self._table.query(
                KeyConditionExpression=Key("PK").eq(f"TASK#{thread_id}") & Key("SK").begins_with("CKPT#"),
                ScanIndexForward=False, Limit=1,
            )
            items = resp.get("Items", [])
            item = items[0] if items else None
        return self._tuple_from_item(thread_id, item) if item else None

    def list(self, config: Optional[dict], *, filter: Optional[dict] = None, before: Optional[dict] = None, limit: Optional[int] = None) -> Iterator[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        resp = self._table.query(
            KeyConditionExpression=Key("PK").eq(f"TASK#{thread_id}") & Key("SK").begins_with("CKPT#"),
            ScanIndexForward=False, Limit=limit or 100,
        )
        for item in resp.get("Items", []):
            yield self._tuple_from_item(thread_id, item)
```

- [x] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_checkpointer.py -v`
Expected: PASS (4 tests). If a method signature doesn't match the installed LangGraph version (per Step 1's inspection), adjust parameter names to match and re-run — do not change the external behavior the tests assert.

- [x] **Step 6: Commit**

```bash
git add app/agent/checkpointer.py tests/test_checkpointer.py
git commit -m "feat: DynamoDB checkpointer matching spec's PK/SK schema"
```

---

### Task 4: GitHub service

**Files:**
- Create: `app/services/github.py`
- Test: `tests/test_github.py`

**Interfaces:**
- Produces: `clone_repo(repo_url: str, dest: Path, token: str) -> None`; `create_branch(repo_path: Path, branch_name: str) -> None`; `commit_all(repo_path: Path, message: str) -> None`; `push_branch(repo_path: Path, branch_name: str, token: str) -> None`; `open_pull_request(repo_url: str, branch: str, base: str, title: str, body: str, token: str) -> str` (returns PR URL).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_github.py
import subprocess
from pathlib import Path

from app.services.github import clone_repo, commit_all, create_branch


def _init_bare_remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "webapp.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=seed, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=seed, check=True)
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@x.com", "-c", "user.name=x", "commit", "-q", "-m", "init"],
        cwd=seed, check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=seed, check=True)
    return bare


def test_clone_repo_clones_local_bare_remote(tmp_path: Path):
    bare = _init_bare_remote(tmp_path)
    dest = tmp_path / "clone"

    clone_repo(str(bare), dest, token="")

    assert (dest / "webapp.py").exists()


def test_create_branch_and_commit_all(tmp_path: Path):
    bare = _init_bare_remote(tmp_path)
    dest = tmp_path / "clone"
    clone_repo(str(bare), dest, token="")

    create_branch(dest, "feature/test")
    (dest / "webapp.py").write_text("x = 2\n")
    commit_all(dest, "bump x")

    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=dest, check=True, capture_output=True, text=True)
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=dest, check=True, capture_output=True, text=True)
    assert log.stdout.strip() == "bump x"
    assert branch.stdout.strip() == "feature/test"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_github.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.github'`

- [ ] **Step 3: Write the implementation**

```python
# app/services/github.py
import subprocess
from pathlib import Path

import httpx


def _with_token(url: str, token: str) -> str:
    if token and url.startswith("https://"):
        return url.replace("https://", f"https://x-access-token:{token}@")
    return url


def clone_repo(repo_url: str, dest: Path, token: str) -> None:
    subprocess.run(["git", "clone", _with_token(repo_url, token), str(dest)], check=True, capture_output=True, text=True)


def create_branch(repo_path: Path, branch_name: str) -> None:
    subprocess.run(["git", "checkout", "-b", branch_name], cwd=repo_path, check=True, capture_output=True, text=True)


def commit_all(repo_path: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git", "-c", "user.email=agent@repomodernizer.local", "-c", "user.name=repomodernizer",
            "commit", "-q", "-m", message, "--allow-empty",
        ],
        cwd=repo_path, check=True, capture_output=True, text=True,
    )


def push_branch(repo_path: Path, branch_name: str, token: str) -> None:
    remote_url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo_path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "remote", "set-url", "origin", _with_token(remote_url, token)],
        cwd=repo_path, check=True, capture_output=True, text=True,
    )
    subprocess.run(["git", "push", "-u", "origin", branch_name], cwd=repo_path, check=True, capture_output=True, text=True)


def _parse_owner_repo(repo_url: str) -> tuple[str, str]:
    cleaned = repo_url.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    parts = cleaned.split("/")
    return parts[-2], parts[-1]


def open_pull_request(repo_url: str, branch: str, base: str, title: str, body: str, token: str) -> str:
    owner, repo = _parse_owner_repo(repo_url)
    response = httpx.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": title, "head": branch, "base": base, "body": body},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["html_url"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_github.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/github.py tests/test_github.py
git commit -m "feat: GitHub clone/branch/commit/push/PR service"
```

---

### Task 5: TaskRunner

**Files:**
- Create: `app/worker/__init__.py`
- Create: `app/worker/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `app.agent.graph.build_graph` (Task 2), `app.agent.checkpointer.DynamoDBCheckpointer` (Task 3), `app.services.github` (Task 4).
- Produces: `RepoContext` dataclass; `TaskRunner(deps_factory, checkpointer, github_token, workspace_root=Path("runs"))` with methods `start(repo_url, goal, test_command, base_branch="main") -> str`, `get_status(task_id: str) -> dict`, `approve(task_id, file, decision, note="") -> None`, `resume(task_id) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runner.py
import json
import threading
import time
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from app.agent.budget import BudgetTracker
from app.agent.nodes import NodeDeps
from app.worker.runner import TaskRunner


class FakeProviderRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt):
        text = self.responses[self.calls]
        self.calls += 1
        return text, 100, 50, "bedrock-primary"


def _deps_factory(responses):
    return lambda: NodeDeps(
        providers=FakeProviderRouter(responses), budget=BudgetTracker(cap_usd=10.0),
        forbidden_paths=[], max_diff_lines=400, risk_threshold=0.6, max_retries=2,
        estimated_cost_per_file=0.01,
    )


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_start_runs_task_to_completion_and_opens_pr(tmp_path, monkeypatch):
    import app.worker.runner as runner_module

    seed = tmp_path / "seed_remote.git"
    _make_bare_remote_with_file(seed, "webapp.py", "x = 1\n")

    pr_calls = []
    monkeypatch.setattr(runner_module.github, "push_branch", lambda *a, **k: None)
    monkeypatch.setattr(
        runner_module.github, "open_pull_request",
        lambda *a, **k: pr_calls.append(a) or "https://github.com/x/y/pull/1",
    )

    responses = [
        json.dumps([{"path": "webapp.py", "rationale": "t", "risk_score": 0.1}]),
        "x = 2\n",
    ]
    runner = TaskRunner(
        deps_factory=_deps_factory(responses), checkpointer=MemorySaver(),
        github_token="", workspace_root=tmp_path / "runs",
    )

    task_id = runner.start(str(seed), "bump x", "true")

    assert _wait_until(lambda: runner.get_status(task_id)["done"])
    status = runner.get_status(task_id)
    assert status["files"]["webapp.py"]["status"] == "migrated"
    assert len(pr_calls) == 1


def test_get_status_reports_awaiting_approval(tmp_path, monkeypatch):
    import app.worker.runner as runner_module

    seed = tmp_path / "seed_remote.git"
    _make_bare_remote_with_file(seed, "webapp.py", "x = 1\n")
    monkeypatch.setattr(runner_module.github, "push_branch", lambda *a, **k: None)
    monkeypatch.setattr(runner_module.github, "open_pull_request", lambda *a, **k: "https://github.com/x/y/pull/1")

    responses = [
        json.dumps([{"path": "webapp.py", "rationale": "t", "risk_score": 0.9}]),
        "x = 2\n",
        "x = 2\n",
    ]
    runner = TaskRunner(
        deps_factory=_deps_factory(responses), checkpointer=MemorySaver(),
        github_token="", workspace_root=tmp_path / "runs",
    )

    task_id = runner.start(str(seed), "bump x", "true")

    assert _wait_until(lambda: runner.get_status(task_id)["awaiting_approval"] is not None)
    status = runner.get_status(task_id)
    assert status["awaiting_approval"]["path"] == "webapp.py"

    runner.approve(task_id, "webapp.py", "approve")

    assert _wait_until(lambda: runner.get_status(task_id)["done"])
    assert runner.get_status(task_id)["files"]["webapp.py"]["status"] == "approved"


def _make_bare_remote_with_file(bare_path: Path, filename: str, content: str) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q", "--bare", str(bare_path)], check=True)
    seed = bare_path.parent / "seed_src"
    seed.mkdir()
    (seed / filename).write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=seed, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_path)], cwd=seed, check=True)
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@x.com", "-c", "user.name=x", "commit", "-q", "-m", "init"],
        cwd=seed, check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=seed, check=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.worker'`

- [ ] **Step 3: Write the implementation**

```python
# app/worker/__init__.py
```

```python
# app/worker/runner.py
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from langgraph.types import Command

from app.agent.graph import build_graph
from app.services import github


@dataclass
class RepoContext:
    path: Path
    repo_url: str
    branch: str
    base_branch: str
    goal: str


class TaskRunner:
    def __init__(self, deps_factory, checkpointer, github_token: str, workspace_root: Path = Path("runs")):
        self.deps_factory = deps_factory
        self.checkpointer = checkpointer
        self.github_token = github_token
        self.workspace_root = workspace_root
        self.errors: dict[str, str] = {}
        self._graphs: dict[str, object] = {}
        self._repo_ctx: dict[str, RepoContext] = {}

    def _graph_for(self, task_id: str):
        if task_id not in self._graphs:
            deps = self.deps_factory()
            graph = build_graph(deps, checkpointer=self.checkpointer)
            config = {"configurable": {"thread_id": task_id}}
            snapshot = graph.get_state(config)
            if snapshot.values:
                deps.budget.cost_used_usd = snapshot.values.get("cost_used_usd", 0.0)
            self._graphs[task_id] = graph
        return self._graphs[task_id]

    def start(self, repo_url: str, goal: str, test_command: str, base_branch: str = "main") -> str:
        task_id = uuid.uuid4().hex[:8]
        workspace = self.workspace_root / task_id / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        github.clone_repo(repo_url, workspace, self.github_token)
        branch = f"repomodernizer/{task_id}"
        github.create_branch(workspace, branch)

        self._repo_ctx[task_id] = RepoContext(
            path=workspace, repo_url=repo_url, branch=branch, base_branch=base_branch, goal=goal,
        )
        config = {"configurable": {"thread_id": task_id}}
        initial_state = {
            "task_id": task_id, "repo_path": str(workspace), "goal": goal,
            "test_command": test_command, "plan": [], "files": {},
            "cursor": 0, "cost_used_usd": 0.0, "trace": [],
        }
        thread = threading.Thread(target=self._run_and_maybe_finalize, args=(task_id, initial_state, config), daemon=True)
        thread.start()
        return task_id

    def _run_and_maybe_finalize(self, task_id: str, invoke_arg, config: dict) -> None:
        try:
            graph = self._graph_for(task_id)
            result = graph.invoke(invoke_arg, config=config)
            if "__interrupt__" in result:
                return
            if any(f["status"] in ("migrated", "approved") for f in result["files"].values()):
                repo_ctx = self._repo_ctx[task_id]
                github.commit_all(repo_ctx.path, f"RepoModernizer: {repo_ctx.goal}")
                github.push_branch(repo_ctx.path, repo_ctx.branch, self.github_token)
                github.open_pull_request(
                    repo_ctx.repo_url, repo_ctx.branch, repo_ctx.base_branch,
                    title=f"RepoModernizer: {repo_ctx.goal}",
                    body="Opened automatically by RepoModernizer.",
                    token=self.github_token,
                )
        except Exception as exc:  # noqa: BLE001
            self.errors[task_id] = str(exc)

    def get_status(self, task_id: str) -> dict:
        graph = self._graph_for(task_id)
        config = {"configurable": {"thread_id": task_id}}
        snapshot = graph.get_state(config)
        awaiting_approval = None
        for task in snapshot.tasks:
            if task.interrupts:
                awaiting_approval = task.interrupts[0].value
        return {
            "task_id": task_id,
            "files": snapshot.values.get("files", {}),
            "cost_used_usd": snapshot.values.get("cost_used_usd", 0.0),
            "awaiting_approval": awaiting_approval,
            "error": self.errors.get(task_id),
            "done": not snapshot.next,
        }

    def approve(self, task_id: str, file: str, decision: str, note: str = "") -> None:
        config = {"configurable": {"thread_id": task_id}}
        thread = threading.Thread(
            target=self._run_and_maybe_finalize,
            args=(task_id, Command(resume={"decision": decision, "note": note}), config),
            daemon=True,
        )
        thread.start()

    def resume(self, task_id: str) -> None:
        config = {"configurable": {"thread_id": task_id}}
        thread = threading.Thread(target=self._run_and_maybe_finalize, args=(task_id, None, config), daemon=True)
        thread.start()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_runner.py -v`
Expected: PASS (2 tests). These hit real Bedrock (via `FakeProviderRouter`, no — they use the fake, so no real Bedrock calls; they're fully offline aside from local git operations).

- [ ] **Step 5: Commit**

```bash
git add app/worker/__init__.py app/worker/runner.py tests/test_runner.py
git commit -m "feat: TaskRunner — background-thread execution, status derivation, GitHub finalize step"
```

---

### Task 6: FastAPI app and routes

**Files:**
- Create: `app/api/__init__.py`
- Create: `app/api/routes_health.py`
- Create: `app/api/routes_tasks.py`
- Create: `app/main.py`
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `app.worker.runner.TaskRunner` (Task 5) via dependency injection (`configure_runner`).
- Produces: FastAPI `app` instance; routes matching spec §6 (`POST /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/approve`, `POST /tasks/{id}/resume`, `GET /health`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_routes.py
from fastapi.testclient import TestClient

from app.api.routes_tasks import configure_runner
from app.main import app


class FakeTaskRunner:
    def __init__(self):
        self.started = []
        self.approved = []
        self.resumed = []

    def start(self, repo_url, goal, test_command, base_branch="main"):
        self.started.append((repo_url, goal, test_command, base_branch))
        return "fake-task-id"

    def get_status(self, task_id):
        return {
            "task_id": task_id, "files": {}, "cost_used_usd": 0.0,
            "awaiting_approval": None, "error": None, "done": True,
        }

    def approve(self, task_id, file, decision, note=""):
        self.approved.append((task_id, file, decision, note))

    def resume(self, task_id):
        self.resumed.append(task_id)


def test_health():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_task_returns_task_id():
    fake = FakeTaskRunner()
    configure_runner(fake)
    client = TestClient(app)

    response = client.post("/tasks", json={
        "repo_url": "https://github.com/x/y", "goal": "migrate", "test_command": "pytest -q",
    })

    assert response.status_code == 200
    assert response.json()["task_id"] == "fake-task-id"
    assert fake.started == [("https://github.com/x/y", "migrate", "pytest -q", "main")]


def test_get_task_status():
    fake = FakeTaskRunner()
    configure_runner(fake)
    client = TestClient(app)

    response = client.get("/tasks/fake-task-id")

    assert response.status_code == 200
    assert response.json()["done"] is True


def test_approve_task_resumes():
    fake = FakeTaskRunner()
    configure_runner(fake)
    client = TestClient(app)

    response = client.post("/tasks/fake-task-id/approve", json={"file": "a.py", "decision": "approve"})

    assert response.status_code == 200
    assert fake.approved == [("fake-task-id", "a.py", "approve", "")]


def test_resume_task():
    fake = FakeTaskRunner()
    configure_runner(fake)
    client = TestClient(app)

    response = client.post("/tasks/fake-task-id/resume")

    assert response.status_code == 200
    assert fake.resumed == ["fake-task-id"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.api'`

- [ ] **Step 3: Write the implementation**

```python
# app/api/__init__.py
```

```python
# app/api/routes_health.py
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok"}
```

```python
# app/api/routes_tasks.py
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
_runner = None


class CreateTaskRequest(BaseModel):
    repo_url: str
    goal: str
    test_command: str
    base_branch: str = "main"


class CreateTaskResponse(BaseModel):
    task_id: str


class ApproveRequest(BaseModel):
    file: str
    decision: str
    note: str = ""


class TaskStatusResponse(BaseModel):
    task_id: str
    files: dict
    cost_used_usd: float
    awaiting_approval: Optional[dict]
    error: Optional[str]
    done: bool


def configure_runner(runner) -> None:
    global _runner
    _runner = runner


def get_runner():
    if _runner is None:
        raise RuntimeError("TaskRunner not configured — call configure_runner() first")
    return _runner


@router.post("/tasks", response_model=CreateTaskResponse)
def create_task(request: CreateTaskRequest):
    runner = get_runner()
    try:
        task_id = runner.start(request.repo_url, request.goal, request.test_command, request.base_branch)
    except Exception as exc:  # noqa: BLE001 - clone/auth failures surface as 422, not a 500 crash
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CreateTaskResponse(task_id=task_id)


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_task(task_id: str):
    return TaskStatusResponse(**get_runner().get_status(task_id))


@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: str, request: ApproveRequest):
    get_runner().approve(task_id, request.file, request.decision, request.note)
    return {"status": "resumed"}


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str):
    get_runner().resume(task_id)
    return {"status": "resumed"}
```

```python
# app/main.py
import boto3
from fastapi import FastAPI

from app.agent.budget import BudgetTracker
from app.agent.checkpointer import DynamoDBCheckpointer
from app.agent.nodes import NodeDeps
from app.agent.providers import BedrockProvider, ProviderRouter
from app.api.routes_health import router as health_router
from app.api.routes_tasks import configure_runner, router as tasks_router
from app.config import Settings
from app.worker.runner import TaskRunner

app = FastAPI(title="RepoModernizer")
app.include_router(health_router)
app.include_router(tasks_router)


def _build_deps(settings: Settings) -> NodeDeps:
    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    providers = ProviderRouter(
        BedrockProvider(settings.bedrock_model_primary, "bedrock-primary", client),
        BedrockProvider(settings.bedrock_model_fallback, "bedrock-fallback", client),
    )
    return NodeDeps(
        providers=providers,
        budget=BudgetTracker(cap_usd=settings.max_task_cost_usd),
        forbidden_paths=settings.forbidden_paths_list(),
        max_diff_lines=settings.max_diff_lines,
        risk_threshold=settings.risk_approval_threshold,
        max_retries=settings.max_file_retries,
        estimated_cost_per_file=settings.estimated_cost_per_file_usd,
    )


@app.on_event("startup")
def _startup() -> None:
    settings = Settings()
    checkpointer = DynamoDBCheckpointer(table_name=settings.ddb_table_checkpoints)
    runner = TaskRunner(
        deps_factory=lambda: _build_deps(settings),
        checkpointer=checkpointer,
        github_token=settings.github_app_token,
    )
    configure_runner(runner)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_routes.py -v`
Expected: PASS (5 tests). Note: importing `app.main` triggers FastAPI's app construction but not `_startup()` (that only runs under a real ASGI server or `TestClient` used as a context manager) — `configure_runner(fake)` in each test overrides whatever `_runner` state exists, so these tests never touch real AWS.

- [ ] **Step 5: Commit**

```bash
git add app/api/__init__.py app/api/routes_health.py app/api/routes_tasks.py app/main.py tests/test_routes.py
git commit -m "feat: FastAPI app and task routes"
```

---

### Task 7: Crash-recovery test (the money shot, automated)

**Files:**
- Test: `tests/test_crash_recovery.py`

**Interfaces:**
- Consumes: everything from Tasks 2, 3, 5 (real `DynamoDBCheckpointer`, `build_graph`, `TaskRunner`).

- [ ] **Step 1: Write the test**

```python
# tests/test_crash_recovery.py
import json
import time
import uuid
from pathlib import Path

import pytest

from app.agent.budget import BudgetTracker
from app.agent.checkpointer import DynamoDBCheckpointer
from app.agent.nodes import NodeDeps
from app.config import Settings
from app.worker.runner import TaskRunner


class FakeProviderRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt):
        text = self.responses[self.calls]
        self.calls += 1
        return text, 100, 50, "bedrock-primary"


def _make_bare_remote(tmp_path: Path, files: dict) -> Path:
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    for name, content in files.items():
        (seed / name).write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=seed, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=seed, check=True)
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@x.com", "-c", "user.name=x", "commit", "-q", "-m", "init"],
        cwd=seed, check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=seed, check=True)
    return bare


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_resume_continues_after_simulated_crash_with_fresh_runner(tmp_path, monkeypatch):
    """The money shot: kill mid-run, spin up a BRAND NEW TaskRunner (simulating a
    process restart — no shared in-memory state), and prove /resume continues
    from the last DynamoDB checkpoint instead of restarting from scratch."""
    import app.worker.runner as runner_module

    settings = Settings()
    checkpoint_table = settings.ddb_table_checkpoints
    remote = _make_bare_remote(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 1\n"})

    monkeypatch.setattr(runner_module.github, "push_branch", lambda *a, **k: None)
    monkeypatch.setattr(runner_module.github, "open_pull_request", lambda *a, **k: "https://github.com/x/y/pull/1")

    responses = [
        json.dumps([
            {"path": "a.py", "rationale": "t", "risk_score": 0.1},
            {"path": "b.py", "rationale": "t", "risk_score": 0.1},
        ]),
        "x = 2\n",  # migrates a.py successfully
        # crash simulated here, before b.py's response is consumed
    ]

    def deps_factory():
        return NodeDeps(
            providers=FakeProviderRouter(responses), budget=BudgetTracker(cap_usd=10.0),
            forbidden_paths=[], max_diff_lines=400, risk_threshold=0.6, max_retries=2,
            estimated_cost_per_file=0.01,
        )

    task_id = f"crash-test-{uuid.uuid4().hex[:8]}"
    checkpointer_1 = DynamoDBCheckpointer(table_name=checkpoint_table)
    runner_1 = TaskRunner(deps_factory=deps_factory, checkpointer=checkpointer_1, github_token="", workspace_root=tmp_path / "runs")

    # start() generates its own task_id; drive the graph directly with our fixed task_id instead
    # so we can reconnect to the exact same thread_id from a second, independent runner below.
    from app.agent.graph import build_graph
    workspace = tmp_path / "runs" / task_id / "workspace"
    workspace.mkdir(parents=True)
    runner_module.github.clone_repo(str(remote), workspace, "")
    runner_module.github.create_branch(workspace, f"repomodernizer/{task_id}")
    runner_1._repo_ctx[task_id] = runner_module.RepoContext(
        path=workspace, repo_url=str(remote), branch=f"repomodernizer/{task_id}", base_branch="main", goal="bump",
    )
    config = {"configurable": {"thread_id": task_id}}
    initial_state = {
        "task_id": task_id, "repo_path": str(workspace), "goal": "bump",
        "test_command": "true", "plan": [], "files": {}, "cursor": 0, "cost_used_usd": 0.0, "trace": [],
    }

    graph_1 = build_graph(deps_factory(), checkpointer=checkpointer_1)
    with pytest.raises(IndexError):
        # b.py's provider response never comes (list exhausted) -> IndexError inside migrate_file_node,
        # simulating an uncaught crash partway through the second file.
        graph_1.invoke(initial_state, config=config)

    # "process restart": brand new checkpointer connection, brand new TaskRunner, no shared memory.
    checkpointer_2 = DynamoDBCheckpointer(table_name=checkpoint_table)
    fresh_responses = ["y = 2\n"]  # only b.py's migration is left to do
    runner_2 = TaskRunner(
        deps_factory=lambda: NodeDeps(
            providers=FakeProviderRouter(fresh_responses), budget=BudgetTracker(cap_usd=10.0),
            forbidden_paths=[], max_diff_lines=400, risk_threshold=0.6, max_retries=2,
            estimated_cost_per_file=0.01,
        ),
        checkpointer=checkpointer_2, github_token="", workspace_root=tmp_path / "runs",
    )
    runner_2._repo_ctx[task_id] = runner_1._repo_ctx[task_id]

    runner_2.resume(task_id)

    assert _wait_until(lambda: runner_2.get_status(task_id)["done"])
    status = runner_2.get_status(task_id)
    assert status["files"]["a.py"]["status"] == "migrated"  # survived the crash, not redone
    assert status["files"]["b.py"]["status"] == "migrated"  # completed after resume
```

- [ ] **Step 2: Run the test**

Run: `.venv/bin/python -m pytest tests/test_crash_recovery.py -v`
Expected: PASS. This is the automated proof of durable crash recovery — record a terminal run of it (or the manual CLI-equivalent demo) as the interview artifact.

- [ ] **Step 3: Commit**

```bash
git add tests/test_crash_recovery.py
git commit -m "test: automated crash-recovery proof — resume continues from last checkpoint after simulated crash"
```

---

### Task 8: Throwaway GitHub repo, live integration test, README update

**Files:**
- Create: `tests/test_github_live.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything from Tasks 3–6 (real `TaskRunner`, `DynamoDBCheckpointer`, GitHub API) — exercises the whole service end to end.

- [ ] **Step 1: Create the throwaway GitHub repo**

```bash
mkdir -p /tmp/repomodernizer-demo-target
cp -r fixtures/sample_repo/* /tmp/repomodernizer-demo-target/
cd /tmp/repomodernizer-demo-target
git init -q
git add -A
git commit -q -m "seed: Flask app for RepoModernizer demo"
gh repo create repomodernizer-demo-target --public --source=. --remote=origin --push
cd -
```

Note the resulting repo URL (e.g. `https://github.com/<your-username>/repomodernizer-demo-target`) — used by the live test and demo below. A fine-grained GitHub PAT with `contents:write` and `pull_requests:write` on this repo, set as `GITHUB_APP_TOKEN` in `.env`, is required for both the live test and the real demo.

- [ ] **Step 2: Write the live integration test**

```python
# tests/test_github_live.py
import os

import boto3
import pytest

from app.agent.budget import BudgetTracker
from app.agent.checkpointer import DynamoDBCheckpointer
from app.agent.nodes import NodeDeps
from app.agent.providers import BedrockProvider, ProviderRouter
from app.config import Settings
from app.worker.runner import TaskRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GITHUB_TESTS") != "1",
    reason="set RUN_LIVE_GITHUB_TESTS=1 to run this test (real Bedrock + real GitHub PR, costs money and opens a real PR)",
)


def test_full_service_migrates_and_opens_real_pr(tmp_path):
    settings = Settings()
    assert settings.github_app_token, "GITHUB_APP_TOKEN must be set for this test"
    demo_repo_url = os.environ["DEMO_REPO_URL"]  # e.g. https://github.com/<you>/repomodernizer-demo-target

    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)

    def deps_factory():
        providers = ProviderRouter(
            BedrockProvider(settings.bedrock_model_primary, "bedrock-primary", client),
            BedrockProvider(settings.bedrock_model_fallback, "bedrock-fallback", client),
        )
        return NodeDeps(
            providers=providers, budget=BudgetTracker(cap_usd=settings.max_task_cost_usd),
            forbidden_paths=settings.forbidden_paths_list(), max_diff_lines=settings.max_diff_lines,
            risk_threshold=settings.risk_approval_threshold, max_retries=settings.max_file_retries,
            estimated_cost_per_file=settings.estimated_cost_per_file_usd,
        )

    runner = TaskRunner(
        deps_factory=deps_factory,
        checkpointer=DynamoDBCheckpointer(table_name=settings.ddb_table_checkpoints),
        github_token=settings.github_app_token,
        workspace_root=tmp_path / "runs",
    )

    task_id = runner.start(
        demo_repo_url, "Migrate this Flask app to FastAPI with async route handlers.", "pytest -q",
    )

    import time
    deadline = time.time() + 180
    status = runner.get_status(task_id)
    while time.time() < deadline and not status["done"] and status["awaiting_approval"] is None:
        time.sleep(2)
        status = runner.get_status(task_id)

    if status["awaiting_approval"] is not None:
        runner.approve(task_id, status["awaiting_approval"]["path"], "approve")
        while time.time() < deadline and not runner.get_status(task_id)["done"]:
            time.sleep(2)
        status = runner.get_status(task_id)

    assert status["done"]
    assert status.get("error") is None
    assert any(f["status"] in ("migrated", "approved") for f in status["files"].values())
```

- [ ] **Step 3: Run the live test**

Run: `RUN_LIVE_GITHUB_TESTS=1 DEMO_REPO_URL=https://github.com/<your-username>/repomodernizer-demo-target .venv/bin/python -m pytest tests/test_github_live.py -v -s`
Expected: PASS. Check the demo repo on GitHub — a real PR should now be open with the migration. This is the full end-to-end proof: real Bedrock, real DynamoDB checkpoint, real GitHub PR.

- [ ] **Step 4: Update the README**

```markdown
# README.md — add a new section after "Run a migration"

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
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_github_live.py README.md
git commit -m "test: live end-to-end service test against a real GitHub PR; README service instructions"
```
