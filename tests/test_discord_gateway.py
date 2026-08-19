import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.discord_gateway import DiscordGateway
from virtizai_core.secrets import FileSecretStore


def test_file_secret_store_is_persistent_and_never_reads_value_back_from_api(tmp_path: Path):
    path = tmp_path / "secrets.json"
    store = FileSecretStore(path)
    store.set("discord-prod", "top-secret-token")
    assert FileSecretStore(path).get("discord-prod") == "top-secret-token"
    assert path.stat().st_mode & 0o077 == 0
    assert "top-secret-token" in path.read_text()


def test_gateway_filtering_normalization_and_chunks(tmp_path: Path):
    from virtizai_core.db import Database
    db = Database(tmp_path / "state.db")
    db.open()
    db.execute("UPDATE discord_config SET enabled=1, allow_dms=0, allowed_servers_json=?, allowed_channels_json=?, allowed_users_json=? WHERE id='discord-default'", (json.dumps(["guild"]), json.dumps(["channel"]), json.dumps(["user"])))
    gateway = DiscordGateway(None, db, FileSecretStore(tmp_path / "secrets.json"))
    assert gateway.is_allowed(guild_id="guild", channel_id="channel", user_id="user")
    assert not gateway.is_allowed(guild_id="other", channel_id="channel", user_id="user")
    assert not gateway.is_allowed(guild_id=None, channel_id="channel", user_id="user")
    assert gateway.normalize_content("<@123> hello", "123", True) == "hello"
    assert gateway.normalize_content("hello", "123", True) is None
    assert gateway.response_chunks("x" * 4001) == ["x" * 2000, "x" * 2000, "x"]


@pytest.mark.asyncio
async def test_secret_api_redacts_value_and_discord_config_persists(tmp_path: Path):
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/v1/secrets/discord-dev", json={"value": "token-value"})
        assert response.status_code == 200
        assert "token-value" not in response.text
        assert (await client.get("/v1/secrets/discord-dev")).json() == {"reference": "discord-dev", "configured": True}
        saved = await client.put("/v1/discord/config", json={"enabled": True, "bot_secret_ref": "discord-dev", "allowed_servers": ["guild"], "allowed_channels": ["channel"], "require_mentions": False})
        assert saved.status_code == 200
        assert saved.json()["bot_secret_configured"] is True
        assert "token-value" not in saved.text
        assert saved.json()["allowed_servers"] == ["guild"]
        status = (await client.get("/v1/discord/status")).json()
        assert status["status"] in {"connecting", "error", "disabled"}


@pytest.mark.asyncio
async def test_disabled_gateway_never_connects(tmp_path: Path):
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = (await client.get("/v1/discord/status")).json()
        assert result["status"] == "disabled"
