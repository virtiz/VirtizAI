from __future__ import annotations

from pathlib import Path

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
