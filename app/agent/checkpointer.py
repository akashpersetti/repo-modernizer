import time
from typing import Any, Iterator, Optional

import boto3
from boto3.dynamodb.conditions import Key
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

_TTL_SECONDS = 14 * 24 * 3600


class DynamoDBCheckpointer(BaseCheckpointSaver):
    def __init__(self, table_name: str, resource=None):
        super().__init__()
        self.serde = JsonPlusSerializer()
        self._table = (resource or boto3.resource("dynamodb")).Table(table_name)

    def _put_blob(self, pk: str, sk: str, obj: Any, extra: Optional[dict] = None) -> None:
        type_, blob = self.serde.dumps_typed(obj)
        item = {"PK": pk, "SK": sk, "type": type_, "blob": blob, "ttl": int(time.time()) + _TTL_SECONDS}
        if extra:
            item.update(extra)
        self._table.put_item(Item=item)

    def _load_blob(self, item: dict) -> Any:
        return self.serde.loads_typed((item["type"], bytes(item["blob"])))

    def put(self, config: dict, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions) -> dict:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = checkpoint["id"]
        parent_id = config["configurable"].get("checkpoint_id")
        self._put_blob(
            f"TASK#{thread_id}", f"CKPT#{checkpoint_id}", (checkpoint, metadata),
            extra={"parent_checkpoint_id": parent_id or ""},
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": config["configurable"].get("checkpoint_ns", ""),
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(self, config: dict, writes: list, task_id: str, task_path: str = "") -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"]["checkpoint_id"]
        for idx, (channel, value) in enumerate(writes):
            self._put_blob(f"TASK#{thread_id}", f"WRITE#{checkpoint_id}#{task_id}#{idx}", (task_id, channel, value))

    def _writes_for(self, thread_id: str, checkpoint_id: str) -> list:
        resp = self._table.query(
            KeyConditionExpression=Key("PK").eq(f"TASK#{thread_id}") & Key("SK").begins_with(f"WRITE#{checkpoint_id}#"),
        )
        return [self._load_blob(item) for item in resp.get("Items", [])]

    def _tuple_from_item(self, thread_id: str, item: dict) -> CheckpointTuple:
        checkpoint, metadata = self._load_blob(item)
        found_id = item["SK"].split("#", 1)[1]
        parent_id = item.get("parent_checkpoint_id") or None
        parent_config = {"configurable": {"thread_id": thread_id, "checkpoint_id": parent_id}} if parent_id else None
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id, "checkpoint_id": found_id}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=self._writes_for(thread_id, found_id),
        )

    def get_tuple(self, config: dict) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"].get("checkpoint_id")
        if checkpoint_id:
            resp = self._table.get_item(Key={"PK": f"TASK#{thread_id}", "SK": f"CKPT#{checkpoint_id}"})
            item = resp.get("Item")
        else:
            resp = self._table.query(
                KeyConditionExpression=Key("PK").eq(f"TASK#{thread_id}") & Key("SK").begins_with("CKPT#"),
                ScanIndexForward=False, Limit=1,
            )
            items = resp.get("Items", [])
            item = items[0] if items else None
        return self._tuple_from_item(thread_id, item) if item else None

    def list(self, config: Optional[dict], *, filter: Optional[dict] = None, before: Optional[dict] = None, limit: Optional[int] = None) -> Iterator[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        resp = self._table.query(
            KeyConditionExpression=Key("PK").eq(f"TASK#{thread_id}") & Key("SK").begins_with("CKPT#"),
            ScanIndexForward=False, Limit=limit or 100,
        )
        for item in resp.get("Items", []):
            yield self._tuple_from_item(thread_id, item)
