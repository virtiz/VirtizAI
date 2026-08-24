from __future__ import annotations

import json
from pathlib import Path

import pytest

from virtizai_core.dev_tools import DevelopmentToolsExecutor
from virtizai_core.db import Database
from virtizai_core.jobs import JobManager
from virtizai_core.orchestration import DelegatedWorkRequest, DelegationService
from virtizai_core.providers import ProviderRegistry
from virtizai_core.registries import EnvironmentRegistry, WorkerRegistry
from virtizai_core.workers import WorkerExecutionBoundary


class TrackingJobs(JobManager):
    def __init__(self, database):
        super().__init__(database)
        self.transitions: list[str] = []

    def transition(self, job_id, status):
        self.transitions.append(status)
        return super().transition(job_id, status)


class CountingBoundary(WorkerExecutionBoundary):
    def __init__(self, database):
        super().__init__(database)
        self.calls = 0

    async def execute(self, request):
        self.calls += 1
        return await super().execute(request)


async def setup(tmp_path: Path):
    database = Database(tmp_path / "state.db"); database.open()
    providers = ProviderRegistry(database)
    session_provider = providers.install_mock_provider("Session fake", ["session-model"])
    delegated_provider = providers.install_mock_provider("Delegated fake", ["delegated-model"])
    await providers.discover_models(session_provider)
    await providers.discover_models(delegated_provider)
    session_model = database.fetch_one("SELECT id FROM models WHERE provider_id=?", (session_provider,))["id"]
    delegated_model = database.fetch_one("SELECT id FROM models WHERE provider_id=?", (delegated_provider,))["id"]
    database.execute("INSERT INTO users(id, display_name) VALUES ('user', 'User')")
    database.execute("INSERT INTO sessions(id, user_id, affinity_provider_id, affinity_model_id) VALUES ('session', 'user', ?, ?)", (session_provider, session_model))
    database.execute("UPDATE roles SET enabled=1 WHERE id='role-coding'")
    workspace = tmp_path / "workspace"; workspace.mkdir()
    worker_id = WorkerRegistry(database).create("Fake dev worker", "dev_tools")
    environment_id = EnvironmentRegistry(database).create("Fake workspace", "workspace")
    database.execute("UPDATE environment_targets SET config_json=? WHERE id=?", (json.dumps({"workspace_path": str(workspace), "allowed_roots": ["src", "tests"]}), environment_id))
    boundary = CountingBoundary(database); boundary.register(DevelopmentToolsExecutor())
    jobs = TrackingJobs(database)
    return database, workspace, worker_id, environment_id, delegated_provider, delegated_model, boundary, jobs, DelegationService(database, jobs, boundary)


def work(worker_id, environment_id, provider_id, model_id, operation, payload):
    return DelegatedWorkRequest("session", "role-coding", provider_id, model_id, worker_id, environment_id, operation, payload, "deterministic integration")


@pytest.mark.asyncio
async def test_end_to_end_fake_components_cover_all_typed_success_paths(tmp_path: Path):
    database, workspace, worker_id, environment_id, provider_id, model_id, boundary, jobs, service = await setup(tmp_path)
    source = workspace / "src" / "sample.txt"; source.parent.mkdir(); source.write_text("before\n")
    inspected = await service.delegate(work(worker_id, environment_id, provider_id, model_id, "inspect_file", {"path": "src/sample.txt", "max_lines": 1}))
    patch = "--- a/src/sample.txt\n+++ b/src/sample.txt\n@@ -1 +1 @@\n-before\n+after\n"
    patched = await service.delegate(work(worker_id, environment_id, provider_id, model_id, "apply_patch", {"patch": patch}))
    tests = workspace / "tests"; tests.mkdir(); (tests / "test_ok.py").write_text("def test_ok(): assert True\n")
    tested = await service.delegate(work(worker_id, environment_id, provider_id, model_id, "run_tests", {"target": "pytest"}))
    assert [job["status"] for job in (inspected, patched, tested)] == ["succeeded", "succeeded", "succeeded"]
    assert boundary.calls == 3 and jobs.transitions == ["running", "succeeded"] * 3
    assert source.read_text() == "after\n" and inspected["result_summary"]
    assert tested["result_json"] and json.loads(tested["result_json"])["output"]["exit_code"] == 0
    assert dict(database.fetch_one("SELECT affinity_provider_id, affinity_model_id FROM sessions WHERE id='session'")) != {"affinity_provider_id": provider_id, "affinity_model_id": model_id}
    assert all(job["provider_id"] == provider_id and job["model_id"] == model_id for job in (inspected, patched, tested))
    metadata = [json.loads(row["metadata_json"]) for row in database.fetch_all("SELECT metadata_json FROM messages WHERE session_id='session'")]
    assert {job["id"] for job in (inspected, patched, tested)} <= {item["job_id"] for item in metadata if item.get("execution_type") == "delegated_job"}
    database.close()


@pytest.mark.asyncio
async def test_end_to_end_typed_failure_is_safe_durable_and_affinity_isolated(tmp_path: Path):
    database, _, worker_id, environment_id, provider_id, model_id, boundary, jobs, service = await setup(tmp_path)
    failed = await service.delegate(work(worker_id, environment_id, provider_id, model_id, "inspect_file", {"path": "../escape"}))
    assert failed["status"] == "failed" and failed["error_summary"] == "Invalid file path"
    assert len(failed["error_summary"]) <= 300 and boundary.calls == 1 and jobs.transitions == ["running", "failed"]
    result = json.loads(failed["result_json"])
    assert result["error_summary"] == "Invalid file path"
    affinity = dict(database.fetch_one("SELECT affinity_provider_id, affinity_model_id FROM sessions WHERE id='session'"))
    assert affinity["affinity_provider_id"] != provider_id and affinity["affinity_model_id"] != model_id
    message = database.fetch_one("SELECT metadata_json FROM messages WHERE session_id='session' ORDER BY rowid DESC")
    assert json.loads(message["metadata_json"]) == {"execution_type": "delegated_job", "job_id": failed["id"], "role_id": "role-coding", "provider_id": provider_id, "model_id": model_id, "worker_id": worker_id, "environment_id": environment_id, "status": "failed"}
    database.close()
