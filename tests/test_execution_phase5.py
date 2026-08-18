from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from virtizai_core.db import Database
from virtizai_core.execution import ExecutionManager, ExecutionPolicy
from virtizai_core.tools import ToolAuthorizationError, ToolRegistryService


@pytest.fixture
def manager(tmp_path: Path) -> ExecutionManager:
    database = Database(tmp_path / "state.db")
    database.open()
    return ExecutionManager(database, tmp_path / "workspace")


@pytest.mark.asyncio
async def test_local_tool_success_structured_audit_and_cleanup(manager: ExecutionManager) -> None:
    tools = ToolRegistryService(manager)
    result = await tools.run("job-success", "host_status", {}, "secretary")
    assert result.status == "SUCCESS"
    assert result.exit_code == 0
    assert result.driver == "local"
    manager.record_audit("user", "job-success", "host_status", result, {"token": "hidden"})
    audit = manager.database.fetch_one("SELECT sanitized_args_json FROM execution_audit")
    assert "hidden" not in audit["sanitized_args_json"]
    assert manager.allocate_workspace("job-success").exists()
    manager.cleanup_workspace("job-success")
    assert not (manager.workspace_dir / "jobs" / "job-success").exists()


@pytest.mark.asyncio
async def test_secretary_cannot_run_sleep_or_file_tool(manager: ExecutionManager) -> None:
    tools = ToolRegistryService(manager)
    with pytest.raises(ToolAuthorizationError):
        await tools.run("job-denied", "sleep", {"seconds": 1}, "secretary")


@pytest.mark.asyncio
async def test_timeout_and_output_caps(manager: ExecutionManager) -> None:
    timeout = await manager.run("job-timeout", "sleep", ["/bin/sleep", "2"], ExecutionPolicy(timeout_seconds=0.05))
    assert timeout.status == "TIMEOUT"
    output = await manager.run("job-output", "output", ["/bin/sh", "-c", "printf 1234567890"], ExecutionPolicy(max_output_bytes=4))
    assert output.status == "SUCCESS"
    assert output.truncated is True
    assert len(output.stdout) <= 4


@pytest.mark.asyncio
async def test_memory_pressure_is_constrained_by_rlimit_as(manager: ExecutionManager) -> None:
    result = await manager.run(
        "job-memory",
        "memory-pressure",
        ["python3", "-c", "bytearray(128 * 1024 * 1024)"],
        ExecutionPolicy(timeout_seconds=5, max_memory_bytes=64 * 1024 * 1024),
    )
    assert result.status == "FAILED"
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_core_remains_responsive_during_heavy_job(manager: ExecutionManager) -> None:
    async def heavy() -> None:
        await manager.run("job-heavy", "sleep", ["/bin/sleep", "0.15"], ExecutionPolicy(timeout_seconds=1))

    task = asyncio.create_task(heavy())
    await asyncio.sleep(0)
    started = asyncio.get_running_loop().time()
    await asyncio.sleep(0)
    assert (asyncio.get_running_loop().time() - started) < 0.05
    await task
