import json
import uuid
from typing import Optional

import boto3
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent.budget import BudgetTracker
from app.agent.checkpointer import DynamoDBCheckpointer
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps
from app.config import Settings

router = APIRouter()
_settings: Optional[Settings] = None
_sqs = None


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
    done: bool
    pr_url: Optional[str]


def configure(settings: Settings, sqs_client=None) -> None:
    global _settings, _sqs
    _settings = settings
    _sqs = sqs_client or boto3.client("sqs", region_name=settings.aws_region)


def _get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("routes_tasks not configured — call configure() first")
    return _settings


def _send(message: dict) -> None:
    _sqs.send_message(QueueUrl=_get_settings().sqs_queue_url, MessageBody=json.dumps(message))


@router.post("/tasks", response_model=CreateTaskResponse)
def create_task(request: CreateTaskRequest):
    if not request.repo_url.startswith("https://github.com/"):
        raise HTTPException(status_code=422, detail="repo_url must be a github.com https URL")
    task_id = uuid.uuid4().hex[:8]
    _send({
        "action": "start", "task_id": task_id, "repo_url": request.repo_url,
        "goal": request.goal, "test_command": request.test_command, "base_branch": request.base_branch,
    })
    return CreateTaskResponse(task_id=task_id)


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_task(task_id: str):
    settings = _get_settings()
    checkpointer = DynamoDBCheckpointer(table_name=settings.ddb_table_checkpoints)
    dummy_deps = NodeDeps(
        providers=None, budget=BudgetTracker(cap_usd=settings.max_task_cost_usd),
        forbidden_paths=[], max_diff_lines=settings.max_diff_lines,
        risk_threshold=settings.risk_approval_threshold, max_retries=settings.max_file_retries,
        estimated_cost_per_file=settings.estimated_cost_per_file_usd,
    )
    graph = build_graph(dummy_deps, checkpointer=checkpointer)
    snapshot = graph.get_state({"configurable": {"thread_id": task_id}})
    awaiting_approval = None
    for task in snapshot.tasks:
        if task.interrupts:
            awaiting_approval = task.interrupts[0].value
    pr_url = checkpointer.get_pr_url(task_id) if hasattr(checkpointer, "get_pr_url") else None
    return TaskStatusResponse(
        task_id=task_id,
        files=snapshot.values.get("files", {}),
        cost_used_usd=snapshot.values.get("cost_used_usd", 0.0),
        awaiting_approval=awaiting_approval,
        done=not snapshot.next,
        pr_url=pr_url,
    )


@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: str, request: ApproveRequest):
    _send({"action": "approve", "task_id": task_id, "decision": request.decision, "note": request.note})
    return {"status": "enqueued"}


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str):
    _send({"action": "resume", "task_id": task_id})
    return {"status": "enqueued"}
