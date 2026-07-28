import json
from unittest.mock import MagicMock, patch

from app.worker.consumer_handler import handler


@patch("app.worker.consumer_handler._ecs")
def test_handler_calls_run_task_per_message(mock_ecs, monkeypatch):
    monkeypatch.setenv("ECS_CLUSTER", "repomod")
    monkeypatch.setenv("ECS_TASK_DEFINITION", "repomod-worker")
    monkeypatch.setenv("SUBNET_IDS", "subnet-1,subnet-2")
    monkeypatch.setenv("SECURITY_GROUP_ID", "sg-1")

    event = {
        "Records": [
            {"body": json.dumps({"action": "start", "task_id": "abc123", "repo_url": "https://x/y"})}
        ]
    }

    result = handler(event, None)

    assert result == {"statusCode": 200}
    mock_ecs.run_task.assert_called_once()
    call_kwargs = mock_ecs.run_task.call_args.kwargs
    assert call_kwargs["cluster"] == "repomod"
    assert call_kwargs["launchType"] == "FARGATE"
    env_overrides = call_kwargs["overrides"]["containerOverrides"][0]["environment"]
    assert {"name": "ACTION", "value": "start"} in env_overrides
    assert {"name": "TASK_ID", "value": "abc123"} in env_overrides
