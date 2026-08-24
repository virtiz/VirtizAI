from __future__ import annotations

from pathlib import Path

import pytest

from virtizai_core.db import Database
from virtizai_core.registries import EnvironmentRegistry, WorkerRegistry
from virtizai_core.workers import ExecutionRequest, ExecutionResult, WorkerExecutionBoundary, WorkerExecutionError


class RecordingExecutor:
    worker_type = "memory"

    def __init__(self, fail: bool = False) -> None:
        self.request = None
        self.fail = fail

    async def execute(self, request, worker, environment):
        self.request = request
        if self.fail:
            raise RuntimeError("internal detail must not surface")
        return ExecutionResult("succeeded", {"operation": request.operation, "payload": request.payload})


def configured(tmp_path: Path, *, worker_type: str = "memory", worker_enabled: bool = True, environment_enabled: bool = True, worker_config: dict | None = None, environment_config: dict | None = None, capabilities: list[str] | None = None):
    database = Database(tmp_path / "state.db"); database.open()
    worker_id = WorkerRegistry(database).create("Worker", worker_type, worker_enabled, "unknown", config=worker_config)
    environment_id = EnvironmentRegistry(database).create("Environment", "workspace")
    database.execute("UPDATE environment_targets SET enabled=?, config_json=?, capabilities_json=? WHERE id=?", (int(environment_enabled), __import__("json").dumps(environment_config or {}), __import__("json").dumps(capabilities or []), environment_id))
    return database, worker_id, environment_id


@pytest.mark.asyncio
async def test_resolves_executor_and_preserves_structured_request(tmp_path: Path):
    database, worker_id, environment_id = configured(tmp_path)
    boundary, executor = WorkerExecutionBoundary(database), RecordingExecutor()
    boundary.register(executor)
    request = ExecutionRequest(worker_id, environment_id, "future.operation", {"nested": {"value": 1}}, 12)
    result = await boundary.execute(request)
    assert executor.request is request
    assert result.status == "succeeded" and result.output == {"operation": "future.operation", "payload": {"nested": {"value": 1}}}
    database.close()


@pytest.mark.asyncio
async def test_rejects_unknown_disabled_missing_and_incompatible_targets(tmp_path: Path):
    database, worker_id, environment_id = configured(tmp_path, worker_type="unknown")
    boundary = WorkerExecutionBoundary(database)
    with pytest.raises(WorkerExecutionError, match="Unknown worker type"): await boundary.execute(ExecutionRequest(worker_id, environment_id, "op"))
    database.execute("UPDATE workers SET enabled=0 WHERE id=?", (worker_id,))
    with pytest.raises(WorkerExecutionError, match="disabled"): await boundary.execute(ExecutionRequest(worker_id, environment_id, "op"))
    database.execute("UPDATE workers SET enabled=1 WHERE id=?", (worker_id,))
    with pytest.raises(WorkerExecutionError, match="Environment not found"): await boundary.execute(ExecutionRequest(worker_id, "missing", "op"))
    database.close()

    database, worker_id, environment_id = configured(tmp_path / "disabled", environment_enabled=False)
    boundary = WorkerExecutionBoundary(database); boundary.register(RecordingExecutor())
    with pytest.raises(WorkerExecutionError, match="Environment is disabled"): await boundary.execute(ExecutionRequest(worker_id, environment_id, "op"))
    database.close()

    database, worker_id, environment_id = configured(tmp_path / "mismatch", worker_config={"required_environment_capabilities": ["workspace"]})
    boundary = WorkerExecutionBoundary(database); boundary.register(RecordingExecutor())
    with pytest.raises(WorkerExecutionError, match="incompatible"): await boundary.execute(ExecutionRequest(worker_id, environment_id, "op"))
    database.close()


@pytest.mark.asyncio
async def test_executor_failure_is_safe_and_execution_does_not_change_session_affinity(tmp_path: Path):
    database, worker_id, environment_id = configured(tmp_path, capabilities=["workspace"])
    database.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user", "User"))
    database.execute("INSERT INTO providers(id, name, adapter_type) VALUES (?, ?, ?)", ("provider", "Provider", "mock"))
    database.execute("INSERT INTO models(id, provider_id, name) VALUES (?, ?, ?)", ("model", "provider", "model"))
    database.execute("INSERT INTO sessions(id, user_id, affinity_provider_id, affinity_model_id) VALUES (?, ?, ?, ?)", ("session", "user", "provider", "model"))
    boundary = WorkerExecutionBoundary(database); boundary.register(RecordingExecutor(fail=True))
    result = await boundary.execute(ExecutionRequest(worker_id, environment_id, "op", {"token": "not surfaced"}))
    assert result.status == "failed" and result.error_summary == "Worker executor failed"
    affinity = database.fetch_one("SELECT affinity_provider_id, affinity_model_id FROM sessions WHERE id='session'")
    assert dict(affinity) == {"affinity_provider_id": "provider", "affinity_model_id": "model"}
    database.close()
