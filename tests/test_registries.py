from __future__ import annotations

import json
from pathlib import Path

import pytest

from virtizai_core.db import Database
from virtizai_core.execution import ExecutionManager
from virtizai_core.registries import (
    ContextBroker,
    EnvironmentRegistry,
    HealthManager,
    IntegrationRegistry,
    MemoryService,
    ProjectRegistry,
    ToolRegistry,
    UpdateManager,
)
from virtizai_core.updates import UpdateCoordinator, UpdateFailure


def release_manifest(version: str = "0.1.1", channel: str = "stable") -> dict:
    return {
        "version": version,
        "channel": channel,
        "release_url": f"https://github.com/virtiz/VirtizAI/releases/tag/v{version}",
        "artifacts": [{"platform": "debian-amd64", "url": f"https://example.invalid/virtizai_{version}_amd64.deb", "sha256": "a" * 64}],
        "classification": {"type": "bugfix", "severity": "low", "breaking": False},
        "minimum_upgrade_version": "0.1.0",
        "schema_compatibility": {"minimum": 1, "maximum": 10},
        "rollback_compatibility": {"supported": True, "requires_data_restore": False},
    }


def test_required_registry_boundaries_have_canonical_storage(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    project_id = ProjectRegistry(database).create("example")
    EnvironmentRegistry(database).create("local", "local")
    ToolRegistry(database).register("status", "Read status", {"type": "object"})
    IntegrationRegistry(database).register("web", "interface")
    MemoryService(database).add("durable fact", "test", project_id=project_id)
    UpdateManager(database).record("0.1.0", "install", "complete")
    attempt_job_id = "job-1"
    database.execute("INSERT INTO jobs(id, kind) VALUES (?, ?)", (attempt_job_id, "test"))
    attempt_id = ExecutionManager(database, tmp_path / "workspace").create_attempt(attempt_job_id)
    assert attempt_id
    assert ContextBroker(database).secretary_context("user", "session")["memory"] == []
    HealthManager(database).set_provider_status("missing", "unknown")
    assert database.fetch_one("SELECT COUNT(*) FROM projects")[0] == 1
    assert database.fetch_one("SELECT COUNT(*) FROM execution_attempts")[0] == 1
    database.close()


def test_update_manager_validates_manifest_and_policy(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    manager = UpdateManager(database)
    imported = manager.import_manifest(release_manifest())
    assert imported["version"] == "0.1.1"
    plan = manager.plan("0.1.0", "debian-amd64")
    assert plan["available"] is True
    assert plan["artifact"]["sha256"] == "a" * 64
    policy = manager.set_policy("stable", "pin_exact", "0.1.1", [])
    assert policy["pinned_version"] == "0.1.1"
    manager.record("0.1.1", "update", "planned", plan["artifact"]["url"])
    assert manager.history()[0]["action"] == "update"
    with pytest.raises(ValueError, match="SHA-256"):
        invalid = release_manifest()
        invalid["artifacts"][0]["sha256"] = "invalid"
        manager.import_manifest(invalid)
    database.close()


def test_update_backup_lock_checksum_and_external_record(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    database.execute("INSERT INTO update_history(id, version, action, status) VALUES ('update-1', '0.1.1', 'update', 'started')")
    coordinator = UpdateCoordinator(database, tmp_path / "data", config_dir=tmp_path / "etc")
    backup = coordinator.backup("update-1", "0.1.0", "0.1.1", 10)
    assert Path(backup["backup_ref"]).is_file()
    assert backup["verified"] is True
    assert coordinator.inspect_backup(backup["backup_ref"], backup["checksum_sha256"])["schema_version"] == 10
    artifact = tmp_path / "artifact.deb"
    artifact.write_text("candidate")
    with pytest.raises(UpdateFailure, match="checksum"):
        coordinator.verify_artifact(artifact, "0" * 64)
    assert coordinator.acquire() is True
    assert coordinator.acquire() is False
    coordinator.release()
    external = coordinator.record_external("0.1.0", "0.1.1", "native_package", 10, "healthy")
    metadata = json.loads(database.fetch_one("SELECT metadata_json FROM update_history WHERE id=?", (external,))["metadata_json"])
    assert metadata["backup_created"] is False
    database.close()


def test_reconciler_refuses_schema_incompatible_pending_update(tmp_path: Path) -> None:
    from virtizai_core.updates import StartupUpdateReconciler
    database = Database(tmp_path / "state.db")
    database.open()
    database.execute("INSERT INTO update_history(id, version, action, status, metadata_json) VALUES ('bad', '0.1.0', 'native_update', 'installed_pending_health', ?)", (json.dumps({"backup": {"verified": True, "schema_version": 999}}),))
    assert StartupUpdateReconciler(database).reconcile("0.1.0", 10) == 0
    assert database.fetch_one("SELECT status FROM update_history WHERE id='bad'")["status"] == "failed"
    database.close()


def test_schema_11_synthetic_transition_is_real(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    database.execute("INSERT INTO app_meta(key, value) VALUES ('synthetic_transition', 'old-value')")
    from virtizai_core.migrations import migration_11
    migration_11(database.connection)
    assert database.fetch_one("SELECT value FROM app_meta WHERE key='synthetic_transition'")["value"] == "schema-11-transformed"
    assert database.fetch_one("SELECT transformed_value FROM schema_transition_proof WHERE id='synthetic-transition'")["transformed_value"] == "schema-11-only"
    database.close()
