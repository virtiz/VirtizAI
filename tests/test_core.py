from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.db import Database
from virtizai_core.jobs import JobManager
from virtizai_core.secrets import EnvironmentSecretStore, MemorySecretStore
from virtizai_core.providers import ProviderRegistry
from virtizai_core.services import CoreService
from virtizai_core.telemetry import TelemetryService


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
        assert health.json()["schema_version"] == 18
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
    assert database.fetch_one("SELECT MAX(version) FROM schema_migrations")[0] == 18
    database.close()

    reopened = Database(tmp_path / "state.db")
    reopened.open()
    assert reopened.fetch_one("SELECT COUNT(*) FROM schema_migrations")[0] == 18
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


@pytest.mark.asyncio
async def test_identity_introspection_uses_session_execution_metadata(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/sessions", json={"user_id": "identity-user"})
        session_id = created.json()["session_id"]
        app.state.core.sessions.add_message(session_id, "assistant", "prior response", {
            "execution_type": "inference", "role": "secretary",
            "provider_name": "Homelab Ollama", "model_name": "phi4-mini:latest",
            "locality": "local", "fallback_used": True,
            "fallback_reason": "primary unavailable",
        })
        response = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"user_id": "identity-user", "content": "Which model is responding to me right now?"},
        )
        assert response.status_code == 200
        assert "without an LLM call" in response.json()["content"]
        assert "Homelab Ollama:phi4-mini:latest" in response.json()["content"]
        assert response.json()["output_tokens"] is None
        row = app.state.database.fetch_one(
            "SELECT metadata_json FROM messages WHERE id = ?", (response.json()["message_id"],)
        )
        assert json.loads(row["metadata_json"])["execution_type"] == "system"


@pytest.mark.asyncio
async def test_previous_deterministic_response_identity_and_natural_variants(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/sessions", json={"user_id": "identity-previous"})
        session_id = created.json()["session_id"]
        first = await client.post(f"/v1/sessions/{session_id}/messages", json={"user_id": "identity-previous", "content": "What is your current routing setup?"})
        assert first.json()["output_tokens"] is None
        second = await client.post(f"/v1/sessions/{session_id}/messages", json={"user_id": "identity-previous", "content": "Which model answered that last message?"})
        assert "generated directly by VirtizAI introspection" in second.json()["content"]
        assert "Tokens: 0" in second.json()["content"]
        assert second.json()["output_tokens"] is None
        assert app.state.core.introspection.is_identity_question("what provider answered that?")
        assert app.state.core.introspection.is_identity_question("did you use the fallback?")
        assert app.state.core.introspection.is_identity_question("what model generated your last response?")


def test_introspection_word_boundaries_reject_normal_local_ai_request(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    introspection = app.state.core.introspection
    assert not introspection.matches("Give me three short reasons local AI is useful in a homelab.")
    assert not introspection.matches("Help me use a local model.")
    assert not introspection.matches("Explain why local AI is useful.")
    assert introspection.matches("Which model answered that last message?")
    assert introspection.matches("What provider handled my previous response?")
    assert introspection.matches("Is Codex available?")


@pytest.mark.asyncio
async def test_unsupported_restart_action_is_truthful_and_zero_token(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/sessions", json={"user_id": "action-user"})
        session_id = created.json()["session_id"]
        response = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"user_id": "action-user", "content": "Restart dev VirtizAI"},
        )
        body = response.json()
        assert "did not perform" in body["content"]
        assert "restarted" not in body["content"].lower()
        assert body["output_tokens"] is None
        row = app.state.database.fetch_one("SELECT metadata_json FROM messages WHERE id = ?", (body["message_id"],))
        metadata = json.loads(row["metadata_json"])
        assert metadata["action_executed"] is False


@pytest.mark.asyncio
async def test_secretary_primary_failure_attempts_fallback_and_records_reason(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    registry = ProviderRegistry(database)
    primary = registry.install_mock_provider("Primary", ["phi"], fail=True)
    fallback = registry.install_mock_provider("Fallback", ["hermes"], fail=False)
    registry.adapters[primary].fail = False
    await registry.discover_models(primary)
    registry.adapters[primary].fail = True
    await registry.discover_models(fallback)
    primary_model = database.fetch_one("SELECT id FROM models WHERE provider_id=?", (primary,))["id"]
    fallback_model = database.fetch_one("SELECT id FROM models WHERE provider_id=?", (fallback,))["id"]
    database.execute("UPDATE providers SET health_status='healthy'")
    database.execute("INSERT INTO routes(id,name,role_id,priority,policy_json) VALUES ('secretary-fallback','Secretary fallback','role-secretary',10,'{}')")
    database.execute("INSERT INTO route_targets(route_id,provider_id,model_id,ordinal) VALUES ('secretary-fallback',?,?,0)", (primary, primary_model))
    database.execute("INSERT INTO route_targets(route_id,provider_id,model_id,ordinal) VALUES ('secretary-fallback',?,?,1)", (fallback, fallback_model))
    core = CoreService(database, TelemetryService(database), JobManager(database), registry)
    core.sessions.ensure_user("fallback-user")
    session_id = core.sessions.create_session("fallback-user")
    response = await core.handle_message("fallback-user", session_id, "hello")
    assert response.provider_name == "Fallback"
    assert response.model_name == "hermes"
    assert response.content.startswith("Fallback")
    row = database.fetch_one("SELECT metadata_json FROM messages WHERE id=?", (response.message_id,))
    metadata = json.loads(row["metadata_json"])
    assert metadata["fallback_used"] is True
    assert metadata["fallback_reason"]
    assert metadata["attempt_failures"][0]["model"] == "phi"
    second = await core.handle_message("fallback-user", session_id, "research why this failed")
    assert second.provider_name == "Fallback"
    affinity = database.fetch_one("SELECT affinity_provider_name, affinity_model_name FROM sessions WHERE id=?", (session_id,))
    assert affinity["affinity_provider_name"] == "Fallback"
    assert affinity["affinity_model_name"] == "hermes"
    database.close()
