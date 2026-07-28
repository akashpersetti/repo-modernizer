from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
_runner = None


class CreateTaskRequest(BaseModel):
    repo_url: str
    goal: str
    test_command: str
    base_branch: str = "main"


class CreateTaskResponse(BaseModel):
    task_id: str


class ApproveRequest(BaseModel):
    file: str
    decision: str
    note: str = ""


class TaskStatusResponse(BaseModel):
    task_id: str
    files: dict
    cost_used_usd: float
    awaiting_approval: Optional[dict]
    error: Optional[str]
    done: bool


def configure_runner(runner) -> None:
    global _runner
    _runner = runner


def get_runner():
    if _runner is None:
        raise RuntimeError("TaskRunner not configured — call configure_runner() first")
    return _runner


@router.post("/tasks", response_model=CreateTaskResponse)
def create_task(request: CreateTaskRequest):
    runner = get_runner()
    try:
        task_id = runner.start(request.repo_url, request.goal, request.test_command, request.base_branch)
    except Exception as exc:  # noqa: BLE001 - clone/auth failures surface as 422, not a 500 crash
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CreateTaskResponse(task_id=task_id)


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_task(task_id: str):
    return TaskStatusResponse(**get_runner().get_status(task_id))


@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: str, request: ApproveRequest):
    get_runner().approve(task_id, request.file, request.decision, request.note)
    return {"status": "resumed"}


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str):
    get_runner().resume(task_id)
    return {"status": "resumed"}
