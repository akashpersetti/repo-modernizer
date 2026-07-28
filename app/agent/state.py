from typing import Literal, Optional, TypedDict


class FileResult(TypedDict):
    path: str
    status: Literal["pending", "migrated", "approved", "rejected", "failed", "skipped"]
    tokens: int
    cost_usd: float
    retry_count: int
    last_error: Optional[str]


class PlanEntry(TypedDict):
    path: str
    rationale: str
    risk_score: float


class GraphState(TypedDict):
    task_id: str
    repo_path: str
    goal: str
    test_command: str
    plan: list[PlanEntry]
    files: dict[str, FileResult]
    cursor: int
    cost_used_usd: float
    trace: list[dict]
    repo_url: str
    branch: str
    base_branch: str
