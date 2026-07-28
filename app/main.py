import boto3
from fastapi import FastAPI

from app.agent.budget import BudgetTracker
from app.agent.checkpointer import DynamoDBCheckpointer
from app.agent.nodes import NodeDeps
from app.agent.providers import BedrockProvider, ProviderRouter
from app.api.routes_health import router as health_router
from app.api.routes_tasks import configure_runner, router as tasks_router
from app.config import Settings
from app.worker.runner import TaskRunner

app = FastAPI(title="RepoModernizer")
app.include_router(health_router)
app.include_router(tasks_router)


def _build_deps(settings: Settings) -> NodeDeps:
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


@app.on_event("startup")
def _startup() -> None:
    settings = Settings()
    checkpointer = DynamoDBCheckpointer(table_name=settings.ddb_table_checkpoints)
    runner = TaskRunner(
        deps_factory=lambda: _build_deps(settings),
        checkpointer=checkpointer,
        github_token=settings.github_app_token,
    )
    configure_runner(runner)
