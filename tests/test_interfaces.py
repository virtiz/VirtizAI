import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.version import __version__


@pytest.mark.asyncio
async def test_interfaces_share_session_and_metadata(tmp_path: Path) -> None:
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        web = await client.post('/v1/interfaces/message', json={'interface_type':'webui','external_subject':'web-user','session_key':'shared','content':'hello'})
        discord = await client.post('/v1/discord/message', json={'interface_type':'discord','external_subject':'discord-user','session_key':'shared','content':'continue'})
        assert web.status_code == 200 and discord.status_code == 200
        assert web.json()['session_id'] != discord.json()['session_id']
        user_id = app.state.database.fetch_one("SELECT user_id FROM interface_identities WHERE interface_type='webui' AND external_subject='web-user'")["user_id"]
        await client.post('/v1/interfaces/link', json={'interface_type':'discord','external_subject':'linked-discord','user_id':user_id})
        linked = await client.post('/v1/discord/message', json={'interface_type':'discord','external_subject':'linked-discord','session_id':web.json()['session_id'],'content':'linked'})
        assert linked.json()['session_id'] == web.json()['session_id']
        assert linked.status_code == 200
        stream = await client.post('/v1/interfaces/stream', json={'interface_type':'webui','external_subject':'web-user','session_key':'stream','content':'stream me'})
        assert stream.status_code == 200
        assert 'event: complete' in stream.text


@pytest.mark.asyncio
async def test_session_ownership_and_discord_config_redaction(tmp_path: Path) -> None:
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put('/v1/secrets/secret-ref', json={'value':'test-token'})
        response = await client.put('/v1/discord/config', json={'enabled':True,'bot_secret_ref':'secret-ref','release_channel_id':'release-channel','admin_users':['admin']})
        assert response.status_code == 200
        config = (await client.get('/v1/discord/config')).json()
        assert config['bot_secret_configured'] is True
        assert 'secret-ref' not in str(config)
        assert (await client.get('/v1/discord/command/status', params={'user_id':'guest'})).json()['status'] == 'ok'
        assert 'releases' in (await client.get('/v1/discord/command/releases', params={'user_id':'guest'})).json()
        assert (await client.get('/v1/discord/command/update', params={'user_id':'guest'})).json()['error'] == 'permission_denied'
        confirmation = (await client.get('/v1/discord/command/update', params={'user_id':'admin'})).json()
        assert confirmation['status'] == 'confirmation_required'
        assert (await client.post(f"/v1/discord/confirm/{confirmation['confirmation_id']}", params={'user_id':'admin'})).json()['action'] == 'update'
        assert (await client.post('/v1/discord/release-event/update_completed', json={'version':'0.1.1'})).json()['event_type'] == 'update_completed'


@pytest.mark.asyncio
async def test_release_api_plans_verified_updates(tmp_path: Path) -> None:
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    manifest = {
        "version": "0.1.30", "channel": "stable", "release_url": "https://example.invalid/releases/v0.1.30",
        "artifacts": [{"platform": "debian-amd64", "url": "https://example.invalid/virtizai.deb", "sha256": "b" * 64}],
        "schema_compatibility": {"minimum": 1, "maximum": 10},
        "rollback_compatibility": {"supported": True, "requires_data_restore": False},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/v1/releases/import", json={"manifest": manifest})).status_code == 200
        plan = await client.get("/v1/updates/plan", params={"platform": "debian-amd64"})
        assert plan.json()["available"] is True
        update = await client.post("/v1/updates/update", params={"platform": "debian-amd64"})
        assert update.status_code == 200
        assert update.json()["status"] == "planned"
        assert (await client.put("/v1/updates/policy", json={"channel": "stable", "version_policy": "pin_exact", "pinned_version": "0.1.1"})).status_code == 200


@pytest.mark.asyncio
async def test_startup_marks_matching_pending_update_known_good(tmp_path: Path) -> None:
    config = AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db")
    app = create_app(config)
    app.state.database.execute("INSERT INTO update_history(id, version, action, status, metadata_json) VALUES ('pending', ?, 'native_update', 'installed_pending_health', ?)", (__version__, json.dumps({"backup": {"verified": True, "schema_version": 16}})))
    restarted = create_app(config)
    assert restarted
    assert app.state.database.fetch_one("SELECT status FROM update_history WHERE id='pending'")["status"] == "known_good"


@pytest.mark.asyncio
async def test_native_update_rejects_unknown_operation(tmp_path: Path) -> None:
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post('/v1/updates/native/unknown', json={"artifact_path":"/tmp/a.deb", "sha256":"0" * 64, "target_version":"0.1.0"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_native_update_failure_and_data_restore_requirement(tmp_path: Path) -> None:
    config = AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db")
    app = create_app(config)
    staging = config.data_dir / "staging"
    staging.mkdir(parents=True)
    artifact = staging / "candidate.deb"
    artifact.write_text("candidate")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing_backup = await client.post('/v1/updates/native/rollback', json={"artifact_path": str(artifact), "sha256": "0" * 64, "target_version": "0.1.0", "restore_data": True})
        assert missing_backup.status_code == 400
        failed = app.state.database.fetch_one("SELECT status, metadata_json FROM update_history ORDER BY created_at DESC LIMIT 1")
        assert failed["status"] == "failed"
        assert json.loads(failed["metadata_json"])["code"] == "unsupported_rollback_baseline"

@pytest.mark.asyncio
async def test_managed_rollback_below_baseline_is_rejected_explicitly(tmp_path: Path) -> None:
    config = AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db")
    app = create_app(config)
    staging = config.data_dir / "staging"
    staging.mkdir(parents=True)
    artifact = staging / "candidate.deb"
    artifact.write_text("candidate")
    checksum = __import__('hashlib').sha256(artifact.read_bytes()).hexdigest()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post('/v1/updates/native/rollback', json={"artifact_path": str(artifact), "sha256": checksum, "target_version": "0.1.17", "target_schema": 10, "restore_data": True})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unsupported_rollback_baseline"

@pytest.mark.asyncio
async def test_application_only_schema_downgrade_is_refused(tmp_path: Path) -> None:
    config = AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db")
    app = create_app(config)
    staging = config.data_dir / "staging"
    staging.mkdir(parents=True)
    artifact = staging / "candidate.deb"
    artifact.write_text("candidate")
    checksum = __import__('hashlib').sha256(artifact.read_bytes()).hexdigest()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post('/v1/updates/native/rollback', json={"artifact_path": str(artifact), "sha256": checksum, "target_version": "0.1.0", "target_schema": 10})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unsupported_rollback_baseline"


@pytest.mark.asyncio
async def test_external_update_records_no_manager_backup(tmp_path: Path) -> None:
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post('/v1/updates/external', json={"old_version":"0.1.0", "new_version":"0.1.1", "source":"docker_compose", "health":"healthy"})
    assert response.status_code == 200
    assert response.json()["backup_created"] is False
