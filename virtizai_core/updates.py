from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .transactions import TransactionJournal
from .db import Database


@dataclass(frozen=True)
class UpdateFailure(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


class NativeUpdateHelper:
    """Fixed-command bridge to the root-owned native updater helper."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable

    def run(self, operation: str, *arguments: str) -> dict:
        if not self.executable:
            raise UpdateFailure("helper_unconfigured", "No native updater helper is configured")
        if operation not in {"backup", "restore", "install"}:
            raise UpdateFailure("helper_operation_denied", "Unsupported helper operation")
        result = subprocess.run([*shlex.split(self.executable), operation, *arguments], check=False, capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise UpdateFailure("helper_failed", result.stderr.strip() or result.stdout.strip() or "Native helper failed")
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("{"):
                return json.loads(line)
        raise UpdateFailure("helper_invalid_response", "Native helper did not return structured JSON")


class UpdateCoordinator:
    """Unprivileged release state, backup, locking, and helper coordination."""

    def __init__(self, database: Database, data_dir: Path, config_dir: Path = Path("/etc/virtizai"), helper: NativeUpdateHelper | None = None) -> None:
        self.database = database
        self.data_dir = data_dir
        self.config_dir = config_dir
        self.helper = helper or NativeUpdateHelper()
        self._lock = threading.Lock()
        self.journal = TransactionJournal(data_dir)

    def acquire(self) -> bool:
        return self._lock.acquire(blocking=False)

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()

    def backup(self, update_id: str, source_version: str, target_version: str, schema_version: int) -> dict:
        backups = self.data_dir / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        archive = backups / f"{update_id}.tar.gz"
        with tempfile.TemporaryDirectory(prefix="virtizai-backup-") as temporary:
            temporary_dir = Path(temporary)
            snapshot = temporary_dir / "virtizai.db"
            if self.database.connection is None:
                raise RuntimeError("Database is not open")
            destination = sqlite3.connect(snapshot)
            self.database.connection.backup(destination)
            destination.close()
            metadata = {
                "update_id": update_id,
                "source_version": source_version,
                "target_version": target_version,
                "schema_version": schema_version,
            }
            (temporary_dir / "metadata.json").write_text(json.dumps(metadata, sort_keys=True))
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(snapshot, arcname="data/virtizai.db")
                bundle.add(temporary_dir / "metadata.json", arcname="metadata.json")
                if self.config_dir.is_dir():
                    bundle.add(self.config_dir, arcname="etc/virtizai")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.database.execute(
            "INSERT INTO update_backups(id, update_id, backup_ref, checksum_sha256, verified) VALUES (?, ?, ?, ?, 1)",
            (str(uuid.uuid4()), update_id, str(archive), digest),
        )
        return {"backup_ref": str(archive), "checksum_sha256": digest, "verified": True, **metadata}

    def verify_artifact(self, artifact: Path, expected_sha256: str) -> None:
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual != expected_sha256:
            raise UpdateFailure("checksum_mismatch", "Artifact checksum does not match the release manifest")

    def inspect_backup(self, backup_ref: str, expected_sha256: str) -> dict:
        """Verify and describe a manager-created rollback archive before restoring it."""
        archive = Path(backup_ref)
        backups = (self.data_dir / "backups").resolve()
        try:
            resolved = archive.resolve(strict=True)
        except OSError as exc:
            raise UpdateFailure("backup_unavailable", "Rollback backup is unavailable") from exc
        if backups not in resolved.parents or resolved.suffixes[-2:] != [".tar", ".gz"]:
            raise UpdateFailure("backup_path_denied", "Rollback backup is outside the VirtizAI backup directory")
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual != expected_sha256:
            raise UpdateFailure("backup_checksum_mismatch", "Rollback backup checksum does not match")
        try:
            with tarfile.open(resolved, "r:gz") as bundle:
                member = bundle.getmember("metadata.json")
                metadata_file = bundle.extractfile(member)
                if metadata_file is None or "data/virtizai.db" not in bundle.getnames():
                    raise UpdateFailure("backup_invalid", "Rollback backup is missing required VirtizAI state")
                metadata = json.loads(metadata_file.read().decode())
        except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise UpdateFailure("backup_invalid", "Rollback backup metadata is invalid") from exc
        if not isinstance(metadata.get("schema_version"), int):
            raise UpdateFailure("backup_invalid", "Rollback backup does not declare its schema version")
        return metadata
    def update_transaction(self, update_id: str, source_version: str, source_schema: int, target_version: str, target_schema: int, backup: dict, artifact_path: str, artifact_sha256: str) -> dict:
        transaction = self.journal.create("native_rollback", transaction_id=update_id, source_version=source_version, source_schema=source_schema, target_version=target_version, target_schema=target_schema, backup=backup, artifact_path=artifact_path, artifact_sha256=artifact_sha256, data_restore_required=True)
        return transaction

    def record_external(self, old_version: str, new_version: str, source: str, schema_version: int, health: str) -> str:
        update_id = str(uuid.uuid4())
        self.database.execute(
            "INSERT INTO update_history(id, version, action, status, release_ref, metadata_json) VALUES (?, ?, 'external_update', ?, ?, ?)",
            (update_id, new_version, health, source, json.dumps({"old_version": old_version, "new_version": new_version, "source": source, "schema_version": schema_version, "backup_created": False})),
        )
        return update_id


class StartupUpdateReconciler:
    """Finalizes only updates that survived restart with the expected schema."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def reconcile(self, running_version: str, schema_version: int) -> int:
        rows = self.database.fetch_all("SELECT id, metadata_json FROM update_history WHERE status='installed_pending_health' AND version=?", (running_version,))
        completed = 0
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            backup = metadata.get("backup", {})
            if not backup.get("verified") or backup.get("schema_version") != schema_version:
                self.database.execute("UPDATE update_history SET status='failed', metadata_json=? WHERE id=?", (json.dumps({**metadata, "code": "startup_reconciliation_failed"}), row["id"]))
                continue
            self.database.execute("UPDATE update_history SET status='known_good' WHERE id=?", (row["id"],))
            completed += 1
        return completed
