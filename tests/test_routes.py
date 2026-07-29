import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.api.routes_tasks import configure
from app.config import Settings
from app.main import app


class FakeSQS:
    def __init__(self):
        self.messages = []

    def send_message(self, QueueUrl, MessageBody):
        self.messages.append({"QueueUrl": QueueUrl, "MessageBody": json.loads(MessageBody)})


def test_health():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_task_returns_task_id():
    fake_sqs = FakeSQS()
    settings = Settings()
    configure(settings, sqs_client=fake_sqs)
    client = TestClient(app)

    response = client.post("/tasks", json={
        "repo_url": "https://github.com/x/y", "goal": "migrate", "test_command": "pytest -q",
    })

    assert response.status_code == 200
    task_id = response.json()["task_id"]
    assert len(task_id) == 8
    assert len(fake_sqs.messages) == 1
    msg = fake_sqs.messages[0]["MessageBody"]
    assert msg["action"] == "start"
    assert msg["repo_url"] == "https://github.com/x/y"
    assert msg["file_extensions"] == ".py"  # default, comma-joined (a plain string, not a JSON list)


def test_create_task_accepts_custom_file_extensions():
    fake_sqs = FakeSQS()
    settings = Settings()
    configure(settings, sqs_client=fake_sqs)
    client = TestClient(app)

    response = client.post("/tasks", json={
        "repo_url": "https://github.com/x/y", "goal": "migrate", "test_command": "npm test",
        "file_extensions": [".js", ".jsx"],
    })

    assert response.status_code == 200
    msg = fake_sqs.messages[0]["MessageBody"]
    assert msg["file_extensions"] == ".js,.jsx"


def test_create_task_rejects_non_github_urls():
    fake_sqs = FakeSQS()
    settings = Settings()
    configure(settings, sqs_client=fake_sqs)
    client = TestClient(app)

    response = client.post("/tasks", json={
        "repo_url": "https://gitlab.com/x/y", "goal": "migrate", "test_command": "pytest -q",
    })

    assert response.status_code == 422


def test_get_task_status():
    with patch("app.api.routes_tasks.DynamoDBCheckpointer", return_value=MemorySaver()):
        fake_sqs = FakeSQS()
        settings = Settings()
        configure(settings, sqs_client=fake_sqs)
        client = TestClient(app)

        response = client.get("/tasks/fake-task-id")

        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert "files" in data
        assert "cost_used_usd" in data
        assert "done" in data


def test_approve_task_enqueues():
    fake_sqs = FakeSQS()
    settings = Settings()
    configure(settings, sqs_client=fake_sqs)
    client = TestClient(app)

    response = client.post("/tasks/fake-task-id/approve", json={"file": "a.py", "decision": "approve"})

    assert response.status_code == 200
    assert response.json()["status"] == "enqueued"
    assert len(fake_sqs.messages) == 1
    msg = fake_sqs.messages[0]["MessageBody"]
    assert msg["action"] == "approve"
    assert msg["task_id"] == "fake-task-id"


def test_resume_task_enqueues():
    fake_sqs = FakeSQS()
    settings = Settings()
    configure(settings, sqs_client=fake_sqs)
    client = TestClient(app)

    response = client.post("/tasks/fake-task-id/resume")

    assert response.status_code == 200
    assert response.json()["status"] == "enqueued"
    assert len(fake_sqs.messages) == 1
    msg = fake_sqs.messages[0]["MessageBody"]
    assert msg["action"] == "resume"


def test_get_task_status_not_done_when_no_checkpoint_exists_yet():
    # Regression: a task_id with no checkpoint at all (enqueued, worker hasn't
    # started/reached ingest_node yet) must NOT read as done. snapshot.next is
    # also an empty tuple in this case, indistinguishable from a truly finished
    # task unless done also requires snapshot.values to be non-empty. Found
    # live: submitting via the dashboard and polling immediately showed
    # "Done -- no files migrated" before the Fargate worker had even started.
    with patch("app.api.routes_tasks.DynamoDBCheckpointer", return_value=MemorySaver()):
        fake_sqs = FakeSQS()
        settings = Settings()
        configure(settings, sqs_client=fake_sqs)
        client = TestClient(app)

        response = client.get("/tasks/never-started-task-id")

        assert response.status_code == 200
        data = response.json()
        assert data["done"] is False
        assert data["files"] == {}


def test_get_task_status_includes_pr_url():
    with patch("app.api.routes_tasks.DynamoDBCheckpointer", return_value=MemorySaver()):
        fake_sqs = FakeSQS()
        settings = Settings()
        configure(settings, sqs_client=fake_sqs)
        client = TestClient(app)

        response = client.get("/tasks/fake-task-id")

        assert response.status_code == 200
        assert "pr_url" in response.json()
        assert response.json()["pr_url"] is None  # MemorySaver has no get_pr_url -- must not crash
