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


def test_ranged_inspection_patch_uses_absolute_file_line_offset(tmp_path):
    import json

    from virtizai_core.dev_tools import DevelopmentToolsExecutor
    from virtizai_core.orchestration import DelegationService
    from virtizai_core.workers import ExecutionRequest

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "sample.txt"
    target.write_text(
        "line1\n"
        "line2\n"
        "line3\n"
        "TARGET old value\n"
        "line5\n"
        "line6\n"
    )

    patch = DelegationService._mutation_patch(
        "sample.txt",
        "TARGET old value",
        "TARGET new value",
        "line3\nTARGET old value\nline5",
        {"sample.txt"},
        3,
    )

    assert "@@ -3,3 +3,3 @@" in patch

    environment = {
        "config_json": json.dumps(
            {
                "workspace_path": str(workspace),
                "allowed_roots": ["."],
            }
        )
    }

    request = ExecutionRequest(
        worker_id="test-worker",
        environment_id="test-env",
        operation="apply_patch",
        payload={"patch": patch, "check_first": True},
        timeout_seconds=30,
    )

    result = DevelopmentToolsExecutor()._apply_patch(request, environment)

    assert result.status == "succeeded"
    assert result.error_summary is None
    assert target.read_text() == (
        "line1\n"
        "line2\n"
        "line3\n"
        "TARGET new value\n"
        "line5\n"
        "line6\n"
    )

@pytest.mark.asyncio
async def test_replace_text_uses_full_file_revision_after_ranged_inspection(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    target = workspace / "src" / "sample.txt"
    target.parent.mkdir()
    target.write_text("one\ntwo\nTARGET old value\nfour\nfive\n")

    inspected = await boundary.execute(
        ExecutionRequest(worker_id, environment_id, "inspect_file",
                         {"path": "src/sample.txt", "start_line": 3, "max_lines": 1})
    )
    assert inspected.status == "succeeded"
    assert inspected.output["content"] == "TARGET old value"
    assert len(inspected.output["revision"]) == 64

    replaced = await boundary.execute(
        ExecutionRequest(
            worker_id, environment_id, "replace_text",
            {
                "path": "src/sample.txt",
                "old_text": "TARGET old value",
                "new_text": "TARGET new value",
                "expected_revision": inspected.output["revision"],
            },
        )
    )

    assert replaced.status == "succeeded"
    assert target.read_text() == "one\ntwo\nTARGET new value\nfour\nfive\n"
    assert replaced.output["current_revision"] == inspected.output["revision"]
    assert replaced.output["result_revision"] != inspected.output["revision"]
    assert "old_text" not in replaced.output
    assert "new_text" not in replaced.output
    assert "replacement" not in replaced.output
    assert "replaced" not in replaced.output
    database.close()


@pytest.mark.asyncio
async def test_replace_text_rejects_stale_inspection(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    target = workspace / "src" / "sample.txt"
    target.parent.mkdir()
    target.write_text("before\n")

    inspected = await boundary.execute(
        ExecutionRequest(worker_id, environment_id, "inspect_file", {"path": "src/sample.txt"})
    )
    target.write_text("changed externally\n")

    result = await boundary.execute(
        ExecutionRequest(
            worker_id, environment_id, "replace_text",
            {
                "path": "src/sample.txt",
                "old_text": "changed externally",
                "new_text": "replacement",
                "expected_revision": inspected.output["revision"],
            },
        )
    )

    assert result.status == "failed"
    assert result.error_summary == "stale_inspection"
    assert target.read_text() == "changed externally\n"
    database.close()


@pytest.mark.asyncio
async def test_replace_text_rejects_missing_and_ambiguous_old_text(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    target = workspace / "src" / "sample.txt"
    target.parent.mkdir()
    target.write_text("same\nsame\n")

    inspected = await boundary.execute(
        ExecutionRequest(worker_id, environment_id, "inspect_file", {"path": "src/sample.txt"})
    )
    revision = inspected.output["revision"]

    empty = await boundary.execute(
        ExecutionRequest(worker_id, environment_id, "replace_text",
                         {"path": "src/sample.txt", "old_text": "", "new_text": "x",
                          "expected_revision": revision})
    )
    assert empty.status == "failed"
    assert empty.error_summary == "old_text_missing"

    missing = await boundary.execute(
        ExecutionRequest(worker_id, environment_id, "replace_text",
                         {"path": "src/sample.txt", "old_text": "absent", "new_text": "x",
                          "expected_revision": revision})
    )
    assert missing.status == "failed"
    assert missing.error_summary == "old_text_missing"

    ambiguous = await boundary.execute(
        ExecutionRequest(worker_id, environment_id, "replace_text",
                         {"path": "src/sample.txt", "old_text": "same", "new_text": "x",
                          "expected_revision": revision})
    )
    assert ambiguous.status == "failed"
    assert ambiguous.error_summary == "old_text_ambiguous"
    assert target.read_text() == "same\nsame\n"
    database.close()


@pytest.mark.asyncio
async def test_replace_text_rejects_paths_outside_allowed_roots(tmp_path: Path):
    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    target = workspace / "src" / "sample.txt"
    target.parent.mkdir()
    target.write_text("inside\n")

    inspected = await boundary.execute(
        ExecutionRequest(worker_id, environment_id, "inspect_file", {"path": "src/sample.txt"})
    )
    revision = inspected.output["revision"]

    for path, expected in (
        ("../secret", "Invalid file path"),
        ("/etc/passwd", "Invalid file path"),
        ("outside.txt", "File path is outside allowed roots"),
    ):
        result = await boundary.execute(
            ExecutionRequest(
                worker_id, environment_id, "replace_text",
                {"path": path, "old_text": "inside", "new_text": "changed",
                 "expected_revision": revision},
            )
        )
        assert result.status == "failed"
        assert result.error_summary == expected

    assert target.read_text() == "inside\n"
    database.close()


@pytest.mark.asyncio
async def test_replace_text_result_revision_matches_written_file(tmp_path: Path):
    import hashlib

    database, workspace, worker_id, environment_id, boundary = setup(tmp_path)
    target = workspace / "tests" / "sample.txt"
    target.parent.mkdir()
    target.write_text("alpha beta gamma\n")

    inspected = await boundary.execute(
        ExecutionRequest(worker_id, environment_id, "inspect_file", {"path": "tests/sample.txt"})
    )

    result = await boundary.execute(
        ExecutionRequest(
            worker_id, environment_id, "replace_text",
            {
                "path": "tests/sample.txt",
                "old_text": "beta",
                "new_text": "delta",
                "expected_revision": inspected.output["revision"],
            },
        )
    )

    written = target.read_text()
    assert result.status == "succeeded"
    assert written == "alpha delta gamma\n"
    assert result.output["result_revision"] == hashlib.sha256(written.encode()).hexdigest()
    database.close()

@pytest.mark.asyncio
async def test_replace_text_rejects_allowed_root_that_escapes_workspace(tmp_path: Path):
    import hashlib

    workspace = tmp_path / "workspace"
    target = workspace / "src" / "sample.txt"
    target.parent.mkdir(parents=True)
    target.write_text("before\n")

    environment = {
        "config_json": json.dumps(
            {
                "workspace_path": str(workspace),
                "allowed_roots": [".."],
            }
        )
    }
    revision = hashlib.sha256(target.read_text().encode()).hexdigest()
    request = ExecutionRequest(
        worker_id="test-worker",
        environment_id="test-env",
        operation="replace_text",
        payload={
            "path": "src/sample.txt",
            "old_text": "before",
            "new_text": "after",
            "expected_revision": revision,
        },
        timeout_seconds=30,
    )

    result = DevelopmentToolsExecutor()._replace_text(request, environment)

    assert result.status == "failed"
    assert result.error_summary == "Environment allowed roots are invalid"
    assert target.read_text() == "before\n"
