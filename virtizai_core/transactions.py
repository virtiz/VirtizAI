"""Durable update transaction records kept outside product SQLite state."""
from __future__ import annotations
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STAGES = frozenset({"REQUESTED", "BACKUP_VERIFIED", "SERVICE_STOPPING", "DATA_RESTORING", "PACKAGE_INSTALLING", "RESTART_PENDING", "POST_RESTART_VALIDATION", "COMPLETED", "FAILED"})

class TransactionJournal:
    def __init__(self, data_dir: Path) -> None:
        self.directory = data_dir / "update-transactions"
        self.directory.mkdir(parents=True, exist_ok=True)
    def path(self, transaction_id: str) -> Path:
        if not transaction_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in transaction_id):
            raise ValueError("invalid transaction id")
        return self.directory / f"{transaction_id}.json"
    def create(self, operation: str, **fields: Any) -> dict[str, Any]:
        transaction = {"transaction_id": str(uuid.uuid4()), "operation": operation, "requested_at": datetime.now(timezone.utc).isoformat(), "stage": "REQUESTED", **fields}
        self.write(transaction)
        return transaction
    def write(self, transaction: dict[str, Any]) -> None:
        if transaction.get("stage") not in STAGES:
            raise ValueError("invalid transaction stage")
        destination = self.path(str(transaction["transaction_id"]))
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(json.dumps(transaction, sort_keys=True) + "\n")
        os.replace(temporary, destination)
    def read(self, transaction_id: str) -> dict[str, Any]:
        return json.loads(self.path(transaction_id).read_text())
    def pending(self) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.directory.glob("*.json")):
            try: records.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError): continue
        return records
    def remove(self, transaction_id: str) -> None:
        self.path(transaction_id).unlink(missing_ok=True)
