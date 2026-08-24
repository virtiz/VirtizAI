from __future__ import annotations

import json
from pathlib import Path

import pytest

from virtizai_core.db import Database
from virtizai_core.dev_tools import DevelopmentToolsExecutor
from virtizai_core.jobs import JobManager
from virtizai_core.orchestration import DelegatedWorkRequest, DelegationError, DelegationService
from virtizai_core.registries import EnvironmentRegistry, WorkerRegistry
from virtizai_core.workers import WorkerExecutionBoundary


def setup(tmp_path: Path, *, role_enabled: bool = True):
    database = Database(tmp_path / "state.db"); database.open()
    database.execute("INSERT INTO users(id, display_name) VALUES ('user', 'User')")
    database.execute("INSERT INTO providers(id, name, adapter_type) VALUES ('session-provider', 'Session', 'mock')")
    database.execute("INSERT INTO providers(id, name, adapter_type) VALUES ('job-provider', 'Job', 'mock')")
    database.execute("INSERT INTO models(id, provider_id, name) VALUES ('session-model', 'session-provider', 'session')")
    database.execute("INSERT INTO models(id, provider_id, name) VALUES ('job-model', 'job-provider', 'job')")
    database.execute("INSERT INTO sessions(id, user_id, affinity_provider_id, affinity_model_id) VALUES ('session', 'user', 'session-provider', 'session-model')")
    database.execute("UPDATE roles SET enabled=? WHERE id='role-coding'", (int(role_enabled),))
    workspace = tmp_path / "workspace"; workspace.mkdir()
    worker_id = WorkerRegistry(database).create("Dev worker", "dev_tools")
    environment_id = EnvironmentRegistry(database).create("Workspace", "workspace")
    database.execute("UPDATE environment_targets SET config_json=? WHERE id=?", (json.dumps({"workspace_path": str(workspace), "allowed_roots": ["src"]}), environment_id))
    boundary = WorkerExecutionBoundary(database); boundary.register(DevelopmentToolsExecutor())
    return database, workspace, worker_id, environment_id, DelegationService(database, JobManager(database), boundary)


def request(worker_id: str, environment_id: str, operation: str = "inspect_file"):
    return DelegatedWorkRequest("session", "role-coding", "job-provider", "job-model", worker_id, environment_id, operation, {"path": "src/source.txt"}, "inspect source")


@pytest.mark.asyncio
async def test_delegation_persists_different_execution_selection_and_session_result(tmp_path: Path):
    database, workspace, worker_id, environment_id, service = setup(tmp_path)
    target = workspace / "src" / "source.txt"; target.parent.mkdir(); target.write_text("content\n")
    job = await service.delegate(request(worker_id, environment_id))
    assert {key: job[key] for key in ("session_id", "role_id", "provider_id", "model_id", "worker_id", "environment_target_id", "status")} == {
        "session_id": "session", "role_id": "role-coding", "provider_id": "job-provider", "model_id": "job-model", "worker_id": worker_id, "environment_target_id": environment_id, "status": "succeeded"}
    assert job["started_at"] and job["finished_at"] and job["result_summary"]
    assert dict(database.fetch_one("SELECT affinity_provider_id, affinity_model_id FROM sessions WHERE id='session'")) == {"affinity_provider_id": "session-provider", "affinity_model_id": "session-model"}
    message = database.fetch_one("SELECT metadata_json FROM messages WHERE session_id='session' ORDER BY rowid DESC")
    assert json.loads(message["metadata_json"])["job_id"] == job["id"]
    database.close()


@pytest.mark.asyncio
async def test_delegation_rejects_invalid_agents_and_failure_is_durable_and_isolated(tmp_path: Path):
    database, workspace, worker_id, environment_id, service = setup(tmp_path)
    with pytest.raises(DelegationError, match="Originating session not found"):
        await service.delegate(DelegatedWorkRequest("missing-session", "role-coding", "job-provider", "job-model", worker_id, environment_id, "inspect_file", {}))
    with pytest.raises(DelegationError, match="Agent not found"):
        await service.delegate(DelegatedWorkRequest("session", "missing", "job-provider", "job-model", worker_id, environment_id, "inspect_file", {}))
    database.execute("UPDATE roles SET enabled=0 WHERE id='role-coding'")
    with pytest.raises(DelegationError, match="Agent is disabled"):
        await service.delegate(request(worker_id, environment_id))
    database.execute("UPDATE roles SET enabled=1 WHERE id='role-coding'")
    failed = await service.delegate(request(worker_id, environment_id, "not-an-operation"))
    assert failed["status"] == "failed" and failed["error_summary"] == "Unsupported development operation"
    assert dict(database.fetch_one("SELECT affinity_provider_id, affinity_model_id FROM sessions WHERE id='session'")) == {"affinity_provider_id": "session-provider", "affinity_model_id": "session-model"}
    database.close()


@pytest.mark.asyncio
async def test_delegation_forwards_bounded_timeout_and_preserves_default(tmp_path: Path):
    database, workspace, worker_id, environment_id, service = setup(tmp_path)
    source = workspace / "src" / "source.txt"; source.parent.mkdir(); source.write_text("content\n")
    captured = []
    original = service.workers.execute
    async def record(execution_request):
        captured.append(execution_request)
        return await original(execution_request)
    service.workers.execute = record
    await service.delegate(DelegatedWorkRequest(
        "session", "role-coding", "job-provider", "job-model", worker_id, environment_id,
        "inspect_file", {"path": "src/source.txt"}, "timed", 12,
    ))
    await service.delegate(request(worker_id, environment_id))
    assert [item.timeout_seconds for item in captured] == [12, None]
    for invalid in (0, -1, 121, "60", True):
        with pytest.raises(DelegationError, match="Invalid delegated execution timeout"):
            await service.delegate(DelegatedWorkRequest(
                "session", "role-coding", "job-provider", "job-model", worker_id, environment_id,
                "inspect_file", {"path": "src/source.txt"}, "invalid", invalid,
            ))
    database.close()
