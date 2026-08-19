from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig


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
        response = await client.put('/v1/discord/config', json={'enabled':True,'bot_secret_ref':'secret-ref','release_channel_id':'release-channel','admin_users':['admin']})
        assert response.status_code == 200
        config = (await client.get('/v1/discord/config')).json()
        assert config['bot_secret_configured'] is True
        assert 'secret-ref' not in str(config)
        assert (await client.get('/v1/discord/command/status', params={'user_id':'guest'})).json()['status'] == 'ok'
        assert (await client.get('/v1/discord/command/update', params={'user_id':'guest'})).json()['error'] == 'permission_denied'


@pytest.mark.asyncio
async def test_release_api_plans_verified_updates(tmp_path: Path) -> None:
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    manifest = {
        "version": "0.1.3", "channel": "stable", "release_url": "https://example.invalid/releases/v0.1.3",
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
