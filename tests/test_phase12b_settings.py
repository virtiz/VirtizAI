import json
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from virtizai_core.api import create_app
from virtizai_core.config import AppConfig

APP = Path(__file__).parents[1] / "webui" / "app.js"

def test_settings_and_integrations_use_backend_state_not_fake_controls():
    app = APP.read_text()
    assert "/v1/preferences" in app
    assert "interface_type:'webui'" in app
    assert "data-setting=\"response\"" in app
    assert "data-action=\"validate-discord\"" in app
    assert "Replace secret</button>" not in app
    assert "localStorage.setItem('virtizai.settings'" not in app

@pytest.mark.asyncio
async def test_webui_preferences_persist_and_discord_config_redacts_secret(tmp_path: Path):
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.get("/v1/interfaces/identity?interface_type=webui&external_subject=webui-user")).json()["user_id"]
        saved = await client.put("/v1/preferences", json={"user_id": identity, "interface_type": "webui", "response_verbosity": "detailed", "execution_updates": "full_trace", "tool_details": "commands_results"})
        assert saved.status_code == 200
        loaded = (await client.get(f"/v1/preferences/{identity}?interface_type=webui")).json()
        assert loaded["response_verbosity"] == "detailed"
        assert loaded["execution_updates"] == "full_trace"
        assert loaded["tool_details"] == "commands_results"
        await client.put("/v1/secrets/discord-test", json={"value": "synthetic-token"})
        config = (await client.put("/v1/discord/config", json={"enabled": False, "bot_secret_ref": "discord-test", "alert_channel_id": "alerts", "allowed_servers": ["guild"]})).json()
        assert config["bot_secret_configured"] is True
        assert "synthetic-token" not in json.dumps(config)
