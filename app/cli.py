import argparse
import uuid
from pathlib import Path
from shutil import copytree

import boto3
from langgraph.types import Command

from app.agent.budget import BudgetTracker
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps
from app.agent.providers import BedrockProvider, ProviderRouter
from app.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="repomod")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--repo", required=True)
    run_parser.add_argument("--goal", required=True)
    run_parser.add_argument("--test-cmd", required=True)
    args = parser.parse_args()

    if args.command == "run":
        run(args.repo, args.goal, args.test_cmd)


def run(repo_path: str, goal: str, test_command: str) -> None:
    settings = Settings()
    task_id = uuid.uuid4().hex[:8]
    run_dir = Path("runs") / task_id
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    copytree(repo_path, workspace, dirs_exist_ok=True)

    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    providers = ProviderRouter(
        BedrockProvider(settings.bedrock_model_primary, "bedrock-primary", client),
        BedrockProvider(settings.bedrock_model_fallback, "bedrock-fallback", client),
    )
    deps = NodeDeps(
        providers=providers,
        budget=BudgetTracker(cap_usd=settings.max_task_cost_usd),
        forbidden_paths=settings.forbidden_paths_list(),
        max_diff_lines=settings.max_diff_lines,
        risk_threshold=settings.risk_approval_threshold,
        max_retries=settings.max_file_retries,
        estimated_cost_per_file=settings.estimated_cost_per_file_usd,
    )
    graph = build_graph(deps)
    config = {"configurable": {"thread_id": task_id}}

    initial_state = {
        "task_id": task_id,
        "repo_path": str(workspace),
        "goal": goal,
        "test_command": test_command,
        "plan": [],
        "files": {},
        "cursor": 0,
        "cost_used_usd": 0.0,
        "trace": [],
    }

    result = graph.invoke(initial_state, config=config)
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print(f"\nRisk gate triggered for {payload['path']} (risk={payload['risk_score']:.2f})")
        print(payload["diff"])
        decision = input("approve/reject: ").strip().lower()
        note = "" if decision == "approve" else input("reason: ").strip()
        result = graph.invoke(Command(resume={"decision": decision, "note": note}), config=config)

    print(f"\nDone. task_id={task_id}, cost=${result['cost_used_usd']:.4f}")
    print(f"Trace + summary written to {run_dir}")


if __name__ == "__main__":
    main()
