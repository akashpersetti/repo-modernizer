# tests/test_graph_integration.py
import os
import shutil
import subprocess
from pathlib import Path

import boto3
import pytest
from langgraph.types import Command

from app.agent.budget import BudgetTracker
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps
from app.agent.providers import BedrockProvider, ProviderRouter
from app.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_BEDROCK_TESTS") != "1",
    reason="set RUN_LIVE_BEDROCK_TESTS=1 to run this test (calls real Bedrock, costs money)",
)


def test_migrate_sample_repo_flask_to_fastapi(tmp_path):
    workspace = tmp_path / "workspace"
    shutil.copytree(Path(__file__).parent.parent / "fixtures" / "sample_repo", workspace)

    settings = Settings()
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
    config = {"configurable": {"thread_id": "integration-test"}}
    initial_state = {
        "task_id": "integration-test",
        "repo_path": str(workspace),
        "goal": "Migrate this Flask app to FastAPI with async route handlers, preserving behavior.",
        "test_command": "pytest -q",
        "plan": [],
        "files": {},
        "cursor": 0,
        "cost_used_usd": 0.0,
        "trace": [],
    }

    result = graph.invoke(initial_state, config=config)
    while "__interrupt__" in result:
        result = graph.invoke(Command(resume={"decision": "approve", "note": "auto-approved in test"}), config=config)

    assert result["files"]["webapp.py"]["status"] in ("migrated", "approved")

    final = subprocess.run(["pytest", "-q"], cwd=workspace, capture_output=True, text=True)
    assert final.returncode == 0, final.stdout + final.stderr
