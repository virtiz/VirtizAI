import pytest
from httpx import ASGITransport, AsyncClient
from virtizai_core.api import create_app
from virtizai_core.config import AppConfig


@pytest.mark.asyncio
async def test_session_history_list_load_and_archive(tmp_path):
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/sessions", json={"user_id": "u1", "title": "Review"})
        sid = created.json()["session_id"]
        listed = await client.get("/v1/sessions", params={"user_id": "u1"})
        assert listed.json()[0]["id"] == sid
        loaded = await client.get(f"/v1/sessions/{sid}")
        assert loaded.json()["title"] == "Review"
        renamed = await client.patch(f"/v1/sessions/{sid}", json={"title": "Renamed", "archived": True})
        assert renamed.json()["status"] == "archived"
        assert (await client.get("/v1/sessions", params={"user_id": "u1"})).json() == []
