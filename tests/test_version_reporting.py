import pytest
from httpx import ASGITransport, AsyncClient
from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.version import __version__

def test_source_checkout_identity(monkeypatch):
    monkeypatch.setenv("VIRTIZAI_DEPLOYMENT", "development")
    monkeypatch.delenv("VIRTIZAI_APP_VERSION", raising=False)
    config = AppConfig.from_environment()
    assert config.app_version.startswith(f"{__version__}-dev+")
    assert config.source_commit
    assert config.deployment == "development"

def test_release_identity_is_immutable(monkeypatch):
    monkeypatch.setenv("VIRTIZAI_DEPLOYMENT", "release")
    monkeypatch.delenv("VIRTIZAI_APP_VERSION", raising=False)
    config = AppConfig.from_environment()
    assert config.app_version == __version__
    assert config.deployment == "release"

@pytest.mark.asyncio
async def test_health_and_webui_version_are_backend_authoritative(tmp_path):
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health = (await client.get("/healthz")).json()
        assert health["version"] == __version__
        assert health["deployment"] == "release"
        assert (await client.get("/")).status_code == 200
