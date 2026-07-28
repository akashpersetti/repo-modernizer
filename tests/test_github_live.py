import os

import boto3
import pytest

from app.agent.budget import BudgetTracker
from app.agent.checkpointer import DynamoDBCheckpointer
from app.agent.nodes import NodeDeps
from app.agent.providers import BedrockProvider, ProviderRouter
from app.config import Settings
from app.worker.runner import TaskRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GITHUB_TESTS") != "1",
    reason="set RUN_LIVE_GITHUB_TESTS=1 to run this test (real Bedrock + real GitHub PR, costs money and opens a real PR)",
)


def test_full_service_migrates_and_opens_real_pr(tmp_path):
    settings = Settings()
    assert settings.github_app_token, "GITHUB_APP_TOKEN must be set for this test"
    demo_repo_url = os.environ["DEMO_REPO_URL"]  # e.g. https://github.com/<you>/repomodernizer-demo-target

    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)

    def deps_factory():
        providers = ProviderRouter(
            BedrockProvider(settings.bedrock_model_primary, "bedrock-primary", client),
            BedrockProvider(settings.bedrock_model_fallback, "bedrock-fallback", client),
        )
        return NodeDeps(
            providers=providers, budget=BudgetTracker(cap_usd=settings.max_task_cost_usd),
            forbidden_paths=settings.forbidden_paths_list(), max_diff_lines=settings.max_diff_lines,
            risk_threshold=settings.risk_approval_threshold, max_retries=settings.max_file_retries,
            estimated_cost_per_file=settings.estimated_cost_per_file_usd,
        )

    runner = TaskRunner(
        deps_factory=deps_factory,
        checkpointer=DynamoDBCheckpointer(table_name=settings.ddb_table_checkpoints),
        github_token=settings.github_app_token,
        workspace_root=tmp_path / "runs",
    )

    task_id = runner.start(
        demo_repo_url, "Migrate this Flask app to FastAPI with async route handlers.", "pytest -q",
    )

    import time
    deadline = time.time() + 180
    status = runner.get_status(task_id)
    while time.time() < deadline and not status["done"] and status["awaiting_approval"] is None:
        time.sleep(2)
        status = runner.get_status(task_id)

    if status["awaiting_approval"] is not None:
        runner.approve(task_id, status["awaiting_approval"]["path"], "approve")
        while time.time() < deadline and not runner.get_status(task_id)["done"]:
            time.sleep(2)
        status = runner.get_status(task_id)

    assert status["done"]
    assert status.get("error") is None
    assert any(f["status"] in ("migrated", "approved") for f in status["files"].values())
