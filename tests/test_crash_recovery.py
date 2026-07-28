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
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare, check=True)
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
