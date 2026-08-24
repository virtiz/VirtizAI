from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig


def config_for(tmp_path: Path) -> AppConfig:
    return AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db")


@pytest.mark.asyncio
async def test_delegated_job_contract_validates_references_and_preserves_session_affinity(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user-1", "User"))
    db.execute("INSERT INTO sessions(id, user_id, affinity_provider_id, affinity_model_id) VALUES (?, ?, ?, ?)", ("session-1", "user-1", "provider-session", "model-session"))
    db.execute("INSERT INTO projects(id, name) VALUES (?, ?)", ("project-1", "Project"))
    db.execute("INSERT INTO roles(id, name, purpose) VALUES (?, ?, ?)", ("role-job", "job-agent", "delegated behavior"))
    db.execute("INSERT INTO providers(id, name, adapter_type) VALUES (?, ?, ?)", ("provider-session", "Session Provider", "test"))
    db.execute("INSERT INTO providers(id, name, adapter_type) VALUES (?, ?, ?)", ("provider-job", "Job Provider", "test"))
    db.execute("INSERT INTO models(id, provider_id, name) VALUES (?, ?, ?)", ("model-session", "provider-session", "session-model"))
    db.execute("INSERT INTO models(id, provider_id, name) VALUES (?, ?, ?)", ("model-job", "provider-job", "job-model"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        worker = (await client.post("/v1/workers", json={"name": "Test worker", "worker_type": "test", "capabilities": ["run"], "config": {"mode": "test"}})).json()
        environment = (await client.post("/v1/environments", json={"name": "Test environment", "target_type": "test", "capabilities": ["workspace"], "config": {"scope": "test"}})).json()
        payload = {
            "kind": "delegated", "payload": {"input": "work"}, "user_id": "user-1", "session_id": "session-1",
            "project_id": "project-1", "role_id": "role-job", "provider_id": "provider-job", "model_id": "model-job",
            "worker_id": worker["id"], "environment_id": environment["id"], "objective": "prove isolation",
        }
        created = await client.post("/v1/jobs", json=payload)
        assert created.status_code == 200
        job_id = created.json()["job_id"]
        job = (await client.get(f"/v1/jobs/{job_id}")).json()
        assert {key: job[key] for key in ("session_id", "project_id", "role_id", "provider_id", "model_id", "worker_id", "environment_target_id", "objective", "status")} == {
            "session_id": "session-1", "project_id": "project-1", "role_id": "role-job", "provider_id": "provider-job", "model_id": "model-job",
            "worker_id": worker["id"], "environment_target_id": environment["id"], "objective": "prove isolation", "status": "queued",
        }
        assert (await client.post("/v1/jobs", json={**payload, "role_id": "missing"})).status_code == 422
        assert (await client.post("/v1/jobs", json={**payload, "provider_id": "missing"})).status_code == 422
        assert (await client.post("/v1/jobs", json={**payload, "model_id": "missing"})).status_code == 422
        assert (await client.post("/v1/jobs", json={**payload, "worker_id": "missing"})).status_code == 422
        assert (await client.post("/v1/jobs", json={**payload, "environment_id": "missing"})).status_code == 422

    affinity = db.fetch_one("SELECT affinity_provider_id, affinity_model_id FROM sessions WHERE id = ?", ("session-1",))
    assert dict(affinity) == {"affinity_provider_id": "provider-session", "affinity_model_id": "model-session"}


@pytest.mark.asyncio
async def test_disabled_targets_rejected_and_job_transitions_are_terminal(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user-1", "User"))
    db.execute("INSERT INTO sessions(id, user_id) VALUES (?, ?)", ("session-1", "user-1"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        disabled_worker = (await client.post("/v1/workers", json={"name": "Disabled worker", "worker_type": "test", "enabled": False})).json()
        disabled_environment = (await client.post("/v1/environments", json={"name": "Disabled environment", "target_type": "test", "enabled": False})).json()
        assert (await client.post("/v1/jobs", json={"kind": "delegated", "session_id": "session-1", "worker_id": disabled_worker["id"]})).status_code == 422
        assert (await client.post("/v1/jobs", json={"kind": "delegated", "session_id": "session-1", "environment_id": disabled_environment["id"]})).status_code == 422

        created = await client.post("/v1/jobs", json={"kind": "delegated", "session_id": "session-1"})
        job_id = created.json()["job_id"]
        running = await client.patch(f"/v1/jobs/{job_id}/status", json={"status": "running"})
        assert running.status_code == 200 and running.json()["started_at"]
        succeeded = await client.patch(f"/v1/jobs/{job_id}/status", json={"status": "succeeded", "result_summary": "done"})
        assert succeeded.status_code == 200 and succeeded.json()["result_summary"] == "done" and succeeded.json()["finished_at"]
        assert (await client.patch(f"/v1/jobs/{job_id}/status", json={"status": "running"})).status_code == 422

        cancelled = await client.post("/v1/jobs", json={"kind": "delegated", "session_id": "session-1"})
        cancelled_id = cancelled.json()["job_id"]
        assert (await client.patch(f"/v1/jobs/{cancelled_id}/status", json={"status": "cancelled"})).status_code == 200
        assert (await client.patch(f"/v1/jobs/{cancelled_id}/status", json={"status": "running"})).status_code == 422
