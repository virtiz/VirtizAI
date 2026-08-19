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
        return json.loads(result.stdout)


class UpdateCoordinator:
    """Unprivileged release state, backup, locking, and helper coordination."""

    def __init__(self, database: Database, data_dir: Path, config_dir: Path = Path("/etc/virtizai"), helper: NativeUpdateHelper | None = None) -> None:
        self.database = database
        self.data_dir = data_dir
        self.config_dir = config_dir
        self.helper = helper or NativeUpdateHelper()
        self._lock = threading.Lock()

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

    def record_external(self, old_version: str, new_version: str, source: str, schema_version: int, health: str) -> str:
        update_id = str(uuid.uuid4())
        self.database.execute(
            "INSERT INTO update_history(id, version, action, status, release_ref, metadata_json) VALUES (?, ?, 'external_update', ?, ?, ?)",
            (update_id, new_version, health, source, json.dumps({"old_version": old_version, "new_version": new_version, "source": source, "schema_version": schema_version, "backup_created": False})),
        )
        return update_id
