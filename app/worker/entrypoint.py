import os
from pathlib import Path

import boto3
from langgraph.types import Command

from app.agent.budget import BudgetTracker
from app.agent.checkpointer import DynamoDBCheckpointer
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps
from app.agent.providers import BedrockProvider, ProviderRouter
from app.config import Settings
from app.services import github


def _default_deps_factory(settings: Settings) -> NodeDeps:
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


def _finalize_if_done(result: dict, token: str) -> None:
    if "__interrupt__" in result:
        return
    if any(f["status"] in ("migrated", "approved") for f in result["files"].values()):
        workspace = Path(result["repo_path"])
        github.commit_all(workspace, f"RepoModernizer: {result['goal']}")
        github.push_branch(workspace, result["branch"], token)
        github.open_pull_request(
            result["repo_url"], result["branch"], result["base_branch"],
            title=f"RepoModernizer: {result['goal']}",
            body="Opened automatically by RepoModernizer.",
            token=token,
        )


def run(checkpointer_factory=None, deps_factory=None, github_token=None) -> None:
    settings = Settings()
    checkpointer = (checkpointer_factory or (lambda: DynamoDBCheckpointer(table_name=settings.ddb_table_checkpoints)))()
    deps = (deps_factory or (lambda: _default_deps_factory(settings)))()
    token = github_token if github_token is not None else settings.github_app_token

    action = os.environ["ACTION"]
    task_id = os.environ["TASK_ID"]
    workspace_root = Path(os.environ.get("WORKSPACE_ROOT", "/mnt/workspace"))
    graph = build_graph(deps, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": task_id}}

    if action == "start":
        repo_url = os.environ["REPO_URL"]
        goal = os.environ["GOAL"]
        base_branch = os.environ.get("BASE_BRANCH") or settings.github_default_base_branch
        branch = f"repomodernizer/{task_id}"
        workspace = workspace_root / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        github.clone_repo(repo_url, workspace, token)
        github.create_branch(workspace, branch)
        initial_state = {
            "task_id": task_id, "repo_path": str(workspace), "goal": goal,
            "test_command": os.environ["TEST_COMMAND"], "plan": [], "files": {},
            "cursor": 0, "cost_used_usd": 0.0, "trace": [],
            "repo_url": repo_url, "branch": branch, "base_branch": base_branch,
        }
        result = graph.invoke(initial_state, config=config)
    elif action == "approve":
        result = graph.invoke(
            Command(resume={"decision": os.environ["DECISION"], "note": os.environ.get("NOTE", "")}),
            config=config,
        )
    elif action == "resume":
        result = graph.invoke(None, config=config)
    else:
        raise ValueError(f"unknown action: {action}")

    _finalize_if_done(result, token)


if __name__ == "__main__":
    run()
