# tests/test_graph.py
import difflib
import json
import subprocess
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agent.budget import BudgetTracker
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps


class FakeProviderRouter:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt: str):
        text = self.responses[self.calls]
        self.calls += 1
        return text, 100, 50, "bedrock-primary"


def _make_diff(before: str, after: str, path: str = "app.py") -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))


def _initial_state(repo_path: str, goal: str, test_command: str) -> dict:
    return {
        "task_id": "test-task",
        "repo_path": repo_path,
        "goal": goal,
        "test_command": test_command,
        "plan": [],
        "files": {},
        "cursor": 0,
        "cost_used_usd": 0.0,
        "trace": [],
        "file_extensions": [".py"],
    }


def test_migrate_graph_happy_path_low_risk(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")

    fake = FakeProviderRouter([
        json.dumps([{"path": "app.py", "rationale": "trivial change", "risk_score": 0.1}]),
        "x = 2\n",
    ])
    deps = NodeDeps(
        providers=fake,
        budget=BudgetTracker(cap_usd=10.0),
        forbidden_paths=[],
        max_diff_lines=400,
        risk_threshold=0.6,
        max_retries=2,
        estimated_cost_per_file=0.01,
    )
    graph = build_graph(deps)
    config = {"configurable": {"thread_id": "happy-path"}}

    result = graph.invoke(_initial_state(str(repo), "bump x", "true"), config=config)

    assert "__interrupt__" not in result
    assert result["files"]["app.py"]["status"] == "migrated"
    assert (repo / "app.py").read_text() == "x = 2\n"


def test_migrate_graph_high_risk_requires_approval(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")

    fake = FakeProviderRouter([
        json.dumps([{"path": "app.py", "rationale": "risky change", "risk_score": 0.9}]),
        "x = 2\n",
        "x = 2\n",
    ])
    deps = NodeDeps(
        providers=fake,
        budget=BudgetTracker(cap_usd=10.0),
        forbidden_paths=[],
        max_diff_lines=400,
        risk_threshold=0.6,
        max_retries=2,
        estimated_cost_per_file=0.01,
    )
    graph = build_graph(deps)
    config = {"configurable": {"thread_id": "high-risk"}}

    result = graph.invoke(_initial_state(str(repo), "bump x", "true"), config=config)
    assert "__interrupt__" in result

    result = graph.invoke(Command(resume={"decision": "approve", "note": ""}), config=config)

    assert "__interrupt__" not in result
    assert result["files"]["app.py"]["status"] == "approved"


def test_migrate_graph_retries_on_apply_failure(tmp_path: Path, monkeypatch):
    import app.agent.nodes as nodes_module

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")

    real_apply_diff = nodes_module.apply_diff
    calls = {"count": 0}

    def flaky_apply_diff(diff_text, workspace_root):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("git apply failed: simulated transient failure")
        return real_apply_diff(diff_text, workspace_root)

    monkeypatch.setattr(nodes_module, "apply_diff", flaky_apply_diff)

    fake = FakeProviderRouter([
        json.dumps([{"path": "app.py", "rationale": "trivial change", "risk_score": 0.1}]),
        "x = 2\n",
        "x = 2\n",
    ])
    deps = NodeDeps(
        providers=fake,
        budget=BudgetTracker(cap_usd=10.0),
        forbidden_paths=[],
        max_diff_lines=400,
        risk_threshold=0.6,
        max_retries=2,
        estimated_cost_per_file=0.01,
    )
    graph = build_graph(deps)
    config = {"configurable": {"thread_id": "apply-failure"}}

    result = graph.invoke(_initial_state(str(repo), "bump x", "true"), config=config)

    assert "__interrupt__" not in result
    assert result["files"]["app.py"]["status"] == "migrated"
    assert result["files"]["app.py"]["retry_count"] == 1
    assert calls["count"] == 2


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
