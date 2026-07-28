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
    time.sleep(0.1)  # small delay to ensure state is fully persisted
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
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare_path, check=True)
