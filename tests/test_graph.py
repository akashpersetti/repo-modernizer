# tests/test_graph.py
import difflib
import json
from pathlib import Path

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
    }


def test_migrate_graph_happy_path_low_risk(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")

    fake = FakeProviderRouter([
        json.dumps([{"path": "app.py", "rationale": "trivial change", "risk_score": 0.1}]),
        _make_diff("x = 1\n", "x = 2\n"),
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
        _make_diff("x = 1\n", "x = 2\n"),
        _make_diff("x = 1\n", "x = 2\n"),
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


def test_migrate_graph_retries_on_malformed_diff(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")

    fake = FakeProviderRouter([
        json.dumps([{"path": "app.py", "rationale": "trivial change", "risk_score": 0.1}]),
        "this is not a valid unified diff",
        _make_diff("x = 1\n", "x = 2\n"),
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
    config = {"configurable": {"thread_id": "malformed-diff"}}

    result = graph.invoke(_initial_state(str(repo), "bump x", "true"), config=config)

    assert "__interrupt__" not in result
    assert result["files"]["app.py"]["status"] == "migrated"
    assert result["files"]["app.py"]["retry_count"] == 1
