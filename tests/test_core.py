from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.db import Database
from virtizai_core.jobs import JobManager
from virtizai_core.secrets import EnvironmentSecretStore, MemorySecretStore


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        log_dir=tmp_path / "logs",
        database_path=tmp_path / "data" / "virtizai.db",
    )


@pytest.mark.asyncio
async def test_health_schema_and_restart_preserve_state(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    app = create_app(config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["schema_version"] == 11
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/static/styles.css")).status_code == 200
        created = await client.post("/v1/sessions", json={"user_id": "user-1"})
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        response = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"user_id": "user-1", "content": "hello"},
        )
        assert response.status_code == 200
        assert response.json()["job_created"] is False

    restarted = create_app(config)
    async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as client:
        schema = await client.get("/v1/schema")
        assert schema.status_code == 200
        assert "sessions" in schema.json()["tables"]
        assert "messages" in schema.json()["tables"]


def test_wal_and_migrations_are_versioned(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    journal_mode = database.fetch_one("PRAGMA journal_mode")[0]
    assert journal_mode.lower() == "wal"
    assert database.fetch_one("SELECT MAX(version) FROM schema_migrations")[0] == 11
    database.close()

    reopened = Database(tmp_path / "state.db")
    reopened.open()
    assert reopened.fetch_one("SELECT COUNT(*) FROM schema_migrations")[0] == 11
    reopened.close()


@pytest.mark.asyncio
async def test_secret_values_never_enter_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    store = MemorySecretStore()
    store.set("provider-main", "not-written-to-db")
    database.execute(
        "INSERT INTO secret_refs(id, name, backend, backend_ref, purpose) VALUES (?, ?, ?, ?, ?)",
        ("ref-1", "provider-main", "memory", "provider-main", "provider credential"),
    )
    rows = database.fetch_all("SELECT * FROM secret_refs")
    assert rows[0]["backend_ref"] == "provider-main"
    assert all("not-written-to-db" not in str(dict(row)) for row in rows)
    assert EnvironmentSecretStore({"PROVIDER_MAIN": "secret"}).get("PROVIDER_MAIN") == "secret"
    database.close()


@pytest.mark.asyncio
async def test_secretary_path_does_not_create_job(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        session_id = (await client.post("/v1/sessions", json={"user_id": "user-1"})).json()["session_id"]
        response = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"user_id": "user-1", "content": "What is the status?"},
        )
        assert response.json()["job_created"] is False
        assert app.state.database.fetch_one("SELECT COUNT(*) FROM jobs")[0] == 0
        stages = app.state.database.fetch_all(
            "SELECT stage FROM telemetry_events WHERE request_id = ?",
            (response.json()["request_id"],),
        )
        assert {row["stage"] for row in stages} >= {"session_lookup", "intent_policy", "message_persist", "secretary_route"}


@pytest.mark.asyncio
async def test_background_job_runs_independently(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    manager = JobManager(database)
    completed = asyncio.Event()

    async def handler(job_id: str, payload: dict) -> dict:
        await asyncio.sleep(0)
        completed.set()
        return {"accepted": payload["value"]}

    manager.register_handler("test", handler)
    job_id = await manager.submit("test", {"value": 7})
    await manager.wait_for_idle()
    assert completed.is_set()
    job = manager.get(job_id)
    assert job is not None
    assert job["status"] == "succeeded"
    database.close()
