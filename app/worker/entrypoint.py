import os
import subprocess
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


def _finalize_if_done(result: dict, token: str, checkpointer) -> None:
    if "__interrupt__" in result:
        return
    # A duplicate/redundant approve or resume action (e.g. a second SQS delivery,
    # or a user re-clicking while a slow Fargate cold start hadn't updated the
    # checkpoint yet) can reach this point more than once for the same task.
    # Without this guard, each duplicate re-opens a PR for a branch that already
    # has one -- GitHub rejects it with a 422, but only after commit_all/push_branch
    # already ran again, producing a duplicate commit each time.
    if hasattr(checkpointer, "get_pr_url") and checkpointer.get_pr_url(result["task_id"]) is not None:
        return
    if any(f["status"] in ("migrated", "approved") for f in result["files"].values()):
        workspace = Path(result["repo_path"])
        github.commit_all(workspace, f"RepoModernizer: {result['goal']}")
        github.push_branch(workspace, result["branch"], token)
        pr_url = github.open_pull_request(
            result["repo_url"], result["branch"], result["base_branch"],
            title=f"RepoModernizer: {result['goal']}",
            body="Opened automatically by RepoModernizer.",
            token=token,
        )
        if hasattr(checkpointer, "put_pr_url"):
            checkpointer.put_pr_url(result["task_id"], pr_url)


def run(checkpointer_factory=None, deps_factory=None, github_token=None) -> None:
    # The EFS access point enforces uid 1000 on every file it writes, regardless of the
    # writing process's actual uid -- but this container runs as root. Git >=2.35.2's
    # dubious-ownership check (CVE-2022-24765) then refuses any git invocation that
    # re-discovers a repo under /mnt/workspace (everything after the process that
    # created it), so every git command must be trusted explicitly.
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", "*"], check=True)

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
        # migrate_file_node re-executes from its own top on every resume (it re-reads
        # the LLM for a fresh diff, re-applies it, re-runs tests) -- calling
        # Command(resume=...) is only safe when there's an actual pending interrupt
        # to satisfy. A duplicate approve action (a second SQS delivery, or the user
        # re-clicking while a slow Fargate cold start hadn't yet updated the
        # checkpoint) has nothing left to resume: the first successful resume already
        # cleared the interrupt. Calling invoke() again anyway doesn't no-op -- it
        # re-runs the node fresh, producing a new duplicate commit each time. Found
        # live: a single approve click produced 6-9 identical commits on the PR.
        snapshot = graph.get_state(config)
        has_pending_interrupt = any(t.interrupts for t in snapshot.tasks)
        # get_state-then-invoke is check-then-act, not atomic -- two callers landing
        # within the same instant (a genuinely concurrent duplicate, not just a
        # sequential re-click) can both read has_pending_interrupt=True before
        # either has resumed. try_claim's conditional put is the actual atomicity:
        # only the first caller to claim this specific checkpoint_id proceeds.
        # Found live: the has_pending_interrupt check alone cut duplicate commits
        # from 6-9 down to 1 extra, not zero, for two approve calls sent ~1s apart.
        checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id", "")
        claimed = (
            checkpointer.try_claim(task_id, f"resume:{checkpoint_id}")
            if hasattr(checkpointer, "try_claim")
            else True
        )
        if has_pending_interrupt and claimed:
            result = graph.invoke(
                Command(resume={"decision": os.environ["DECISION"], "note": os.environ.get("NOTE", "")}),
                config=config,
            )
        else:
            result = snapshot.values
    elif action == "resume":
        result = graph.invoke(None, config=config)
    else:
        raise ValueError(f"unknown action: {action}")

    _finalize_if_done(result, token, checkpointer)


if __name__ == "__main__":
    run()
