# Framework-agnostic on purpose: passes against both the original Flask webapp.py
# and a migrated FastAPI webapp.py, so this file itself is excluded from the
# migration plan (see nodes.py:_is_test_file) and stays fixed as the source of truth.
from webapp import app as application


def _client():
    if hasattr(application, "test_client"):
        return application.test_client()
    from fastapi.testclient import TestClient
    return TestClient(application)


def _json(response):
    if hasattr(response, "get_json"):
        return response.get_json()
    return response.json()


def test_health():
    client = _client()
    response = client.get("/health")
    assert response.status_code == 200
    assert _json(response) == {"status": "ok"}


def test_get_item():
    client = _client()
    response = client.get("/items/42")
    assert response.status_code == 200
    assert _json(response) == {"id": 42, "name": "item-42"}
