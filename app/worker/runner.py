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
        self._in_progress: set[str] = set()

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
        self._in_progress.add(task_id)
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
        finally:
            self._in_progress.discard(task_id)

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
            "done": not snapshot.next and task_id not in self._in_progress,
        }

    def approve(self, task_id: str, file: str, decision: str, note: str = "") -> None:
        config = {"configurable": {"thread_id": task_id}}
        self._in_progress.add(task_id)
        thread = threading.Thread(
            target=self._run_and_maybe_finalize,
            args=(task_id, Command(resume={"decision": decision, "note": note}), config),
            daemon=True,
        )
        thread.start()

    def resume(self, task_id: str) -> None:
        config = {"configurable": {"thread_id": task_id}}
        self._in_progress.add(task_id)
        thread = threading.Thread(target=self._run_and_maybe_finalize, args=(task_id, None, config), daemon=True)
        thread.start()
