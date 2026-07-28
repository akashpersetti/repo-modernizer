import uuid

from app.agent.checkpointer import DynamoDBCheckpointer
from app.config import Settings

_settings = Settings()
_checkpointer = DynamoDBCheckpointer(table_name=_settings.ddb_table_checkpoints)


def _sample_checkpoint(checkpoint_id: str) -> dict:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"cursor": 0, "files": {}},
        "channel_versions": {},
        "versions_seen": {},
    }


def test_put_and_get_tuple_roundtrip():
    thread_id = f"test-{uuid.uuid4().hex[:8]}"
    checkpoint = _sample_checkpoint("ckpt-1")
    config = {"configurable": {"thread_id": thread_id}}

    _checkpointer.put(config, checkpoint, {"step": 0}, {})
    result = _checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})

    assert result is not None
    assert result.checkpoint["id"] == "ckpt-1"
    assert result.checkpoint["channel_values"]["cursor"] == 0


def test_get_tuple_returns_none_for_unknown_thread():
    result = _checkpointer.get_tuple({"configurable": {"thread_id": f"nonexistent-{uuid.uuid4().hex}"}})
    assert result is None


def test_list_returns_newest_first():
    thread_id = f"test-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    _checkpointer.put(config, _sample_checkpoint("ckpt-a"), {"step": 0}, {})
    _checkpointer.put(config, _sample_checkpoint("ckpt-b"), {"step": 1}, {})

    results = list(_checkpointer.list({"configurable": {"thread_id": thread_id}}))

    assert [r.checkpoint["id"] for r in results] == ["ckpt-b", "ckpt-a"]


def test_put_writes_surfaces_as_pending_writes():
    thread_id = f"test-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    _checkpointer.put(config, _sample_checkpoint("ckpt-1"), {"step": 0}, {})

    write_config = {"configurable": {"thread_id": thread_id, "checkpoint_id": "ckpt-1"}}
    _checkpointer.put_writes(write_config, [("files", {"a.py": "pending"})], task_id="task-1")

    result = _checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})

    assert result.pending_writes == [["task-1", "files", {"a.py": "pending"}]]
