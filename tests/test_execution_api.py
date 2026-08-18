from pathlib import Path
from time import perf_counter

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.execution import ExecutionPolicy


@pytest.mark.asyncio
async def test_tool_api_returns_job_and_structured_result(tmp_path: Path) -> None:
    config = AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db")
    app = create_app(config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/tools/run", json={"tool": "host_status", "profile": "secretary"})
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        await app.state.execution.active[job_id]
        job = (await client.get(f"/v1/jobs/{job_id}")).json()
        assert job["status"] == "success"
        assert "status" in job["result_json"]
        activity = await client.get("/v1/activity")
        assert activity.status_code == 200


@pytest.mark.asyncio
async def test_health_stays_responsive_during_memory_pressure(tmp_path: Path) -> None:
    config = AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db")
    app = create_app(config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        pressure = __import__("asyncio").create_task(app.state.execution.run("api-memory", "memory-pressure", ["python3", "-c", "bytearray(128 * 1024 * 1024)"], ExecutionPolicy(timeout_seconds=5, max_memory_bytes=64 * 1024 * 1024)))
        started = perf_counter()
        health = await client.get("/healthz")
        elapsed_ms = (perf_counter() - started) * 1000
        assert health.status_code == 200
        assert elapsed_ms < 100
        result = await pressure
        assert result.status == "FAILED"
