from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig


@pytest.mark.asyncio
async def test_dashboard_uses_persisted_metrics(tmp_path: Path) -> None:
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/dashboard")
        assert response.status_code == 200
        assert response.json()["secretary_response_count"] == 0
        session = (await client.post("/v1/sessions", json={"user_id": "dashboard-test"})).json()["session_id"]
        await client.post(f"/v1/sessions/{session}/messages", json={"user_id": "dashboard-test", "content": "hello"})
        dashboard = (await client.get("/v1/dashboard")).json()
        assert dashboard["secretary_response_count"] == 1
        assert dashboard["secretary_latency_ms"] is not None
