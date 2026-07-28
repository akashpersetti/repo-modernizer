from fastapi.testclient import TestClient

from app.api.routes_tasks import configure_runner
from app.main import app


class FakeTaskRunner:
    def __init__(self):
        self.started = []
        self.approved = []
        self.resumed = []

    def start(self, repo_url, goal, test_command, base_branch="main"):
        self.started.append((repo_url, goal, test_command, base_branch))
        return "fake-task-id"

    def get_status(self, task_id):
        return {
            "task_id": task_id, "files": {}, "cost_used_usd": 0.0,
            "awaiting_approval": None, "error": None, "done": True,
        }

    def approve(self, task_id, file, decision, note=""):
        self.approved.append((task_id, file, decision, note))

    def resume(self, task_id):
        self.resumed.append(task_id)


def test_health():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_task_returns_task_id():
    fake = FakeTaskRunner()
    configure_runner(fake)
    client = TestClient(app)

    response = client.post("/tasks", json={
        "repo_url": "https://github.com/x/y", "goal": "migrate", "test_command": "pytest -q",
    })

    assert response.status_code == 200
    assert response.json()["task_id"] == "fake-task-id"
    assert fake.started == [("https://github.com/x/y", "migrate", "pytest -q", "main")]


def test_get_task_status():
    fake = FakeTaskRunner()
    configure_runner(fake)
    client = TestClient(app)

    response = client.get("/tasks/fake-task-id")

    assert response.status_code == 200
    assert response.json()["done"] is True


def test_approve_task_resumes():
    fake = FakeTaskRunner()
    configure_runner(fake)
    client = TestClient(app)

    response = client.post("/tasks/fake-task-id/approve", json={"file": "a.py", "decision": "approve"})

    assert response.status_code == 200
    assert fake.approved == [("fake-task-id", "a.py", "approve", "")]


def test_resume_task():
    fake = FakeTaskRunner()
    configure_runner(fake)
    client = TestClient(app)

    response = client.post("/tasks/fake-task-id/resume")

    assert response.status_code == 200
    assert fake.resumed == ["fake-task-id"]
