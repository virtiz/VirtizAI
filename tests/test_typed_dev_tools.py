from __future__ import annotations

import json
from pathlib import Path

import pytest

from virtizai_core.db import Database
from virtizai_core.dev_tools import DevelopmentToolsExecutor
from virtizai_core.registries import EnvironmentRegistry, WorkerRegistry
from virtizai_core.workers import ExecutionRequest, WorkerExecutionBoundary, WorkerExecutionError


def setup(tmp_path: Path, *, worker_enabled: bool = True, environment_enabled: bool = True):
    database = Database(tmp_path / "state.db"); database.open()
    workspace = tmp_path / "workspace"; workspace.mkdir()
    worker_id = WorkerRegistry(database).create("Dev worker", "dev_tools", worker_enabled)
    environment_id = EnvironmentRegistry(database).create("Workspace", "workspace")
    database.execute("UPDATE environment_targets SET enabled=?, config_json=? WHERE id=?", (int(environment_enabled), json.dumps({"workspace_path": str(workspace), "allowed_roots": ["src", "tests"]}), environment_id))
    boundary = WorkerExecutionBoundary(database); boundary.register(DevelopmentToolsExecutor())
    return database, workspace, worker_id, environment_id, boundary


@pytest.mark.asyncio
async def test_inspect_file_is_bounded_and_uses_boundary(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    target = workspace / "src" / "sample.py"; target.parent.mkdir(); target.write_text("one\ntwo\nthree\nfour\n")
    result = await boundary.execute(ExecutionRequest(worker_id, environment_id, "inspect_file", {"path": "src/sample.py", "start_line": 2, "max_lines": 2}))
    assert result.status == "succeeded"
    assert result.output["lines"] == ["two", "three"]
    database.close()


@pytest.mark.asyncio
async def test_list_files_is_bounded_to_persisted_allowed_roots(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    (workspace / "src").mkdir(); (workspace / "src" / "sample.py").write_text("x")
    (workspace / "tests").mkdir(); (workspace / "tests" / "test_sample.py").write_text("x")
    (workspace / "outside.py").write_text("x")
    result = await boundary.execute(ExecutionRequest(worker_id, environment_id, "list_files", {}))
    assert result.status == "succeeded" and result.output["files"] == ["src/sample.py", "tests/test_sample.py"]
    rejected = await boundary.execute(ExecutionRequest(worker_id, environment_id, "list_files", {"path": "src"}))
    assert rejected.status == "failed" and rejected.error_summary == "list_files does not accept arguments"
    database.close()


@pytest.mark.asyncio
async def test_inspect_file_rejects_escape_and_missing_files(tmp_path: Path):
    database, _, worker_id, environment_id, boundary = setup(tmp_path)
    for path, expected in (("../secret", "Invalid file path"), ("/etc/passwd", "Invalid file path"), ("src/missing.py", "File not found")):
        result = await boundary.execute(ExecutionRequest(worker_id, environment_id, "inspect_file", {"path": path}))
        assert result.status == "failed" and result.error_summary == expected
    database.close()


@pytest.mark.asyncio
async def test_run_tests_allowed_target_failure_timeout_and_bounded_output(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    tests = workspace / "tests"; tests.mkdir()
    (tests / "test_pass.py").write_text("def test_ok(): assert True\n")
    passed = await boundary.execute(ExecutionRequest(worker_id, environment_id, "run_tests", {"target": "pytest"}))
    assert passed.status == "succeeded" and passed.output["exit_code"] == 0
    unsupported = await boundary.execute(ExecutionRequest(worker_id, environment_id, "run_tests", {"target": "shell"}))
    assert unsupported.error_summary == "Unsupported test target"
    (tests / "test_failure.py").write_text("def test_fail(): assert False\n")
    failed = await boundary.execute(ExecutionRequest(worker_id, environment_id, "run_tests", {"target": "pytest"}))
    assert failed.status == "failed" and failed.output["exit_code"] != 0
    (tests / "test_failure.py").unlink()
    (tests / "test_slow.py").write_text("import time\ndef test_slow(): time.sleep(1)\n")
    timed_out = await boundary.execute(ExecutionRequest(worker_id, environment_id, "run_tests", {"target": "pytest"}, 0.01))
    assert timed_out.status == "failed" and timed_out.error_summary == "Test execution timed out"
    (tests / "test_slow.py").unlink()
    (tests / "test_output.py").write_text("def test_output(): print('x' * 5000); assert False\n")
    bounded = await boundary.execute(ExecutionRequest(worker_id, environment_id, "run_tests", {"target": "pytest"}))
    assert len(bounded.output["stdout"].encode()) <= DevelopmentToolsExecutor.max_output_bytes
    assert bounded.output["stdout_truncated"] is True
    database.close()


@pytest.mark.asyncio
async def test_packet3_validation_and_session_affinity_are_preserved(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    with pytest.raises(WorkerExecutionError, match="Worker is disabled"):
        database.execute("UPDATE workers SET enabled=0 WHERE id=?", (worker_id,))
        await boundary.execute(ExecutionRequest(worker_id, environment_id, "inspect_file", {"path": "src/x"}))
    database.execute("UPDATE workers SET enabled=1 WHERE id=?", (worker_id,))
    database.execute("UPDATE environment_targets SET enabled=0 WHERE id=?", (environment_id,))
    with pytest.raises(WorkerExecutionError, match="Environment is disabled"):
        await boundary.execute(ExecutionRequest(worker_id, environment_id, "inspect_file", {"path": "src/x"}))
    database.execute("UPDATE environment_targets SET enabled=1 WHERE id=?", (environment_id,))
    database.execute("INSERT INTO users(id, display_name) VALUES ('user', 'User')")
    database.execute("INSERT INTO providers(id, name, adapter_type) VALUES ('provider', 'Provider', 'mock')")
    database.execute("INSERT INTO models(id, provider_id, name) VALUES ('model', 'provider', 'model')")
    database.execute("INSERT INTO sessions(id, user_id, affinity_provider_id, affinity_model_id) VALUES ('session', 'user', 'provider', 'model')")
    (workspace / "src").mkdir(); (workspace / "src" / "x").write_text("x")
    await boundary.execute(ExecutionRequest(worker_id, environment_id, "inspect_file", {"path": "src/x"}))
    assert dict(database.fetch_one("SELECT affinity_provider_id, affinity_model_id FROM sessions WHERE id='session'")) == {"affinity_provider_id": "provider", "affinity_model_id": "model"}
    database.close()


@pytest.mark.asyncio
async def test_apply_patch_uses_boundary_and_applies_checked_modify_only_patch(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    target = workspace / "src" / "sample.txt"; target.parent.mkdir(); target.write_text("one\ntwo\n")
    patch = "--- a/src/sample.txt\n+++ b/src/sample.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+patched\n"
    result = await boundary.execute(ExecutionRequest(worker_id, environment_id, "apply_patch", {"patch": patch}))
    assert result.status == "succeeded" and result.output == {"files_changed": 1, "check_first": True}
    assert target.read_text() == "one\npatched\n"
    database.close()


@pytest.mark.asyncio
async def test_apply_patch_rejects_invalid_targets_and_never_partially_applies(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    first = workspace / "src" / "one.txt"; first.parent.mkdir(); first.write_text("one\n")
    second = workspace / "src" / "two.txt"; second.write_text("two\n")
    bad_multi = "--- a/src/one.txt\n+++ b/src/one.txt\n@@ -1 +1 @@\n-one\n+changed\n--- a/src/two.txt\n+++ b/src/two.txt\n@@ -1 +1 @@\n-wrong\n+changed\n"
    failed = await boundary.execute(ExecutionRequest(worker_id, environment_id, "apply_patch", {"patch": bad_multi}))
    assert failed.status == "failed" and first.read_text() == "one\n" and second.read_text() == "two\n"
    for patch, error in (
        ("not a patch", "Malformed patch"),
        ("--- /etc/passwd\n+++ /etc/passwd\n@@ -1 +1 @@\n-x\n+y\n", "Invalid patch target"),
        ("--- a/../secret\n+++ b/../secret\n@@ -1 +1 @@\n-x\n+y\n", "Invalid patch target"),
        ("--- a/other/x\n+++ b/other/x\n@@ -1 +1 @@\n-x\n+y\n", "Invalid patch target"),
    ):
        result = await boundary.execute(ExecutionRequest(worker_id, environment_id, "apply_patch", {"patch": patch}))
        assert result.status == "failed" and result.error_summary == error
    database.execute("UPDATE workers SET enabled=0 WHERE id=?", (worker_id,))
    with pytest.raises(WorkerExecutionError, match="Worker is disabled"):
        await boundary.execute(ExecutionRequest(worker_id, environment_id, "apply_patch", {"patch": bad_multi}))
    database.execute("UPDATE workers SET enabled=1 WHERE id=?", (worker_id,))
    database.execute("UPDATE environment_targets SET enabled=0 WHERE id=?", (environment_id,))
    with pytest.raises(WorkerExecutionError, match="Environment is disabled"):
        await boundary.execute(ExecutionRequest(worker_id, environment_id, "apply_patch", {"patch": bad_multi}))
    database.close()


@pytest.mark.asyncio
async def test_run_tests_packet5_target_is_fixed_and_accepts_no_injected_arguments(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    tests = workspace / "tests"; tests.mkdir()
    (tests / "test_job_orchestration.py").write_text("def test_packet5_target(): assert True\n")
    result = await boundary.execute(ExecutionRequest(worker_id, environment_id, "run_tests", {"target": "packet5", "args": ["-k", "injected"]}, 10))
    assert result.status == "succeeded" and result.output["exit_code"] == 0
    assert DevelopmentToolsExecutor.test_targets["packet5"] == (__import__("sys").executable, "-m", "pytest", "-q", "tests/test_job_orchestration.py")
    database.close()


@pytest.mark.asyncio
async def test_run_tests_allows_one_existing_focused_test_without_pytest_arguments(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    tests = workspace / "tests"; tests.mkdir()
    (tests / "test_focused.py").write_text("def test_ok(): assert True\n")
    result = await boundary.execute(ExecutionRequest(worker_id, environment_id, "run_tests", {"target": "tests/test_focused.py"}))
    assert result.status == "succeeded" and result.output["target"] == "tests/test_focused.py"
    rejected = await boundary.execute(ExecutionRequest(worker_id, environment_id, "run_tests", {"target": "tests/test_focused.py -k injected"}))
    assert rejected.status == "failed" and rejected.error_summary == "Unsupported test target"
    database.close()
