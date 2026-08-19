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
class StartupTransactionReconciler:
    """Persist completed helper transactions into the database after restart."""
    def __init__(self, database: Any, journal: TransactionJournal) -> None:
        self.database = database
        self.journal = journal
    def reconcile(self, running_version: str, schema_version: int) -> int:
        completed = 0
        for transaction in self.journal.pending():
            if transaction.get("stage") != "RESTART_PENDING":
                continue
            if transaction.get("target_version") != running_version or transaction.get("target_schema") != schema_version:
                continue
            metadata = {"transaction": transaction, "backup": transaction["backup"], "data_restore_required": True}
            existing = self.database.fetch_one("SELECT id FROM update_history WHERE id=?", (transaction["transaction_id"],))
            if existing is None:
                self.database.execute("INSERT INTO update_history(id, version, action, status, release_ref, metadata_json) VALUES (?, ?, 'native_rollback', 'known_good', ?, ?)", (transaction["transaction_id"], running_version, transaction.get("artifact_path"), json.dumps(metadata, sort_keys=True)))
            self.journal.remove(transaction["transaction_id"])
            completed += 1
        return completed
