import json
import os

from langgraph.checkpoint.memory import MemorySaver

from app.agent.budget import BudgetTracker
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps
from app.worker import entrypoint


class FakeProviderRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt):
        if self.calls < len(self.responses):
            text = self.responses[self.calls]
        else:
            text = self.responses[-1] if self.responses else "pass"
        self.calls += 1
        return text, 100, 50, "bedrock-primary"


def _make_bare_remote(tmp_path):
    import subprocess
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
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare, check=True)
    return bare


def test_start_then_separate_approve_survives_fresh_container(tmp_path, monkeypatch):
    """The exact scenario that motivated EFS: 'start' and 'approve' run as two
    separate calls (simulating two separate containers) sharing only a checkpointer
    and an on-disk workspace root -- not any in-memory object."""
    import app.worker.entrypoint as ep

    remote = _make_bare_remote(tmp_path)
    checkpointer = MemorySaver()
    pr_calls = []
    monkeypatch.setattr(ep.github, "push_branch", lambda *a, **k: None)
    monkeypatch.setattr(ep.github, "open_pull_request", lambda *a, **k: pr_calls.append(a) or "https://x/pull/1")

    task_id = "entrypoint-test-1"
    responses_start = [
        json.dumps([{"path": "webapp.py", "rationale": "t", "risk_score": 0.9}]),
        "x = 2\n",
        "pass",
        "pass",
        "pass",
    ]

    def deps_factory():
        return NodeDeps(
            providers=FakeProviderRouter(responses_start), budget=BudgetTracker(cap_usd=10.0),
            forbidden_paths=[], max_diff_lines=400, risk_threshold=0.6, max_retries=2,
            estimated_cost_per_file=0.01,
        )

    env = {
        "ACTION": "start", "TASK_ID": task_id, "REPO_URL": str(remote),
        "GOAL": "bump x", "TEST_COMMAND": "true", "WORKSPACE_ROOT": str(tmp_path / "workspace_root"),
    }
    monkeypatch.setattr(os, "environ", {**os.environ, **env})
    ep.run(checkpointer_factory=lambda: checkpointer, deps_factory=deps_factory, github_token="")

    # "approve" now runs as a fresh call with a fresh NodeDeps/FakeProviderRouter
    # (simulating a brand new container) -- only the checkpointer and the
    # WORKSPACE_ROOT filesystem path are shared, exactly as EFS provides in prod.
    env2 = {
        "ACTION": "approve", "TASK_ID": task_id, "DECISION": "approve", "NOTE": "",
        "WORKSPACE_ROOT": str(tmp_path / "workspace_root"),
    }
    monkeypatch.setattr(os, "environ", {**os.environ, **env2})
    ep.run(
        checkpointer_factory=lambda: checkpointer,
        deps_factory=lambda: NodeDeps(
            providers=FakeProviderRouter([]), budget=BudgetTracker(cap_usd=10.0),
            forbidden_paths=[], max_diff_lines=400, risk_threshold=0.6, max_retries=2,
            estimated_cost_per_file=0.01,
        ),
        github_token="",
    )

    graph = build_graph(deps_factory(), checkpointer=checkpointer)
    snapshot = graph.get_state({"configurable": {"thread_id": task_id}})
    assert snapshot.values["files"]["webapp.py"]["status"] == "approved"
    assert len(pr_calls) == 1
