from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .workers import ExecutionRequest, ExecutionResult


class DevelopmentToolsExecutor:
    """A generic development worker which dispatches only bounded operations."""

    worker_type = "dev_tools"
    max_lines = 200
    max_output_bytes = 4000
    test_targets = {
        "pytest": (sys.executable, "-m", "pytest", "-q"),
        "packet5": (sys.executable, "-m", "pytest", "-q", "tests/test_job_orchestration.py"),
    }

    @staticmethod
    def _config(environment: dict) -> dict[str, Any]:
        import json
        try:
            value = json.loads(environment.get("config_json") or "{}")
        except json.JSONDecodeError:
            value = {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _workspace(environment: dict) -> Path | None:
        path = DevelopmentToolsExecutor._config(environment).get("workspace_path")
        if not isinstance(path, str) or not path:
            return None
        try:
            return Path(path).resolve()
        except OSError:
            return None

    async def execute(self, request: ExecutionRequest, worker: dict, environment: dict) -> ExecutionResult:
        if request.operation == "inspect_file":
            return self._inspect_file(request, environment)
        if request.operation == "run_tests":
            return await self._run_tests(request, environment)
        if request.operation == "apply_patch":
            return self._apply_patch(request, environment)
        return ExecutionResult("failed", error_summary="Unsupported development operation")

    def _inspect_file(self, request: ExecutionRequest, environment: dict) -> ExecutionResult:
        workspace = self._workspace(environment)
        payload = request.payload
        path_value = payload.get("path")
        if workspace is None or not workspace.is_dir():
            return ExecutionResult("failed", error_summary="Environment workspace is unavailable")
        if not isinstance(path_value, str) or not path_value or Path(path_value).is_absolute() or ".." in Path(path_value).parts:
            return ExecutionResult("failed", error_summary="Invalid file path")
        allowed_roots = self._config(environment).get("allowed_roots", ["."])
        if not isinstance(allowed_roots, list) or not all(isinstance(root, str) for root in allowed_roots):
            return ExecutionResult("failed", error_summary="Environment allowed roots are invalid")
        candidate = (workspace / path_value).resolve()
        roots = [(workspace / root).resolve() for root in allowed_roots]
        if workspace not in candidate.parents and candidate != workspace:
            return ExecutionResult("failed", error_summary="Invalid file path")
        if not any(candidate == root or root in candidate.parents for root in roots):
            return ExecutionResult("failed", error_summary="File path is outside allowed roots")
        if not candidate.is_file():
            return ExecutionResult("failed", error_summary="File not found")
        start = payload.get("start_line", 1)
        end = payload.get("end_line")
        maximum = payload.get("max_lines", self.max_lines)
        if not all(isinstance(value, int) and value > 0 for value in (start, maximum)) or end is not None and (not isinstance(end, int) or end < start):
            return ExecutionResult("failed", error_summary="Invalid line bounds")
        maximum = min(maximum, self.max_lines)
        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start - 1:(end if end is not None else start - 1 + maximum)]
        selected = selected[:maximum]
        return ExecutionResult("succeeded", {"path": path_value, "start_line": start, "lines": selected, "content": "\n".join(selected), "truncated": len(selected) == maximum})

    def _patch_target(self, raw: str, workspace: Path, roots: list[Path]) -> Path | None:
        name = raw.split("\t", 1)[0]
        if name.startswith(("a/", "b/")):
            name = name[2:]
        relative = Path(name)
        if not name or name == "/dev/null" or relative.is_absolute() or ".." in relative.parts:
            return None
        target = (workspace / relative).resolve()
        if (workspace not in target.parents and target != workspace) or not any(root == target or root in target.parents for root in roots):
            return None
        return target

    def _apply_patch(self, request: ExecutionRequest, environment: dict) -> ExecutionResult:
        """Apply a modify-only unified diff after planning every hunk before writes."""
        workspace = self._workspace(environment)
        patch = request.payload.get("patch")
        if workspace is None or not workspace.is_dir():
            return ExecutionResult("failed", error_summary="Environment workspace is unavailable")
        if not isinstance(patch, str) or not patch or len(patch.encode()) > 200_000:
            return ExecutionResult("failed", error_summary="Malformed patch")
        allowed = self._config(environment).get("allowed_roots", ["."])
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            return ExecutionResult("failed", error_summary="Environment allowed roots are invalid")
        roots = [(workspace / item).resolve() for item in allowed]
        lines = patch.splitlines()
        planned: dict[Path, str] = {}
        position = 0
        try:
            while position < len(lines):
                if position + 2 >= len(lines) or not lines[position].startswith("--- ") or not lines[position + 1].startswith("+++ "):
                    raise ValueError("Malformed patch")
                old_target = self._patch_target(lines[position][4:], workspace, roots)
                new_target = self._patch_target(lines[position + 1][4:], workspace, roots)
                if old_target is None or new_target is None or old_target != new_target or not old_target.is_file():
                    raise ValueError("Invalid patch target")
                target = old_target
                source = planned.get(target, target.read_text(encoding="utf-8", errors="replace"))
                source_lines = source.splitlines()
                output: list[str] = []
                cursor = 0
                position += 2
                hunk_count = 0
                while position < len(lines) and not lines[position].startswith("--- "):
                    match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@$", lines[position])
                    if not match:
                        raise ValueError("Malformed patch hunk")
                    old_start, old_count, new_count = int(match.group(1)), int(match.group(2) or "1"), int(match.group(4) or "1")
                    if old_start < 1 or old_start - 1 < cursor:
                        raise ValueError("Invalid patch hunk")
                    output.extend(source_lines[cursor:old_start - 1])
                    cursor = old_start - 1
                    position += 1
                    old_seen = new_seen = 0
                    while position < len(lines) and not lines[position].startswith(("@@ ", "--- ")):
                        line = lines[position]
                        if not line or line[0] not in " +-":
                            raise ValueError("Malformed patch hunk")
                        content = line[1:]
                        if line[0] in " -":
                            if cursor >= len(source_lines) or source_lines[cursor] != content:
                                raise ValueError("Patch check failed")
                            cursor += 1
                            old_seen += 1
                        if line[0] in " +":
                            output.append(content)
                            new_seen += 1
                        position += 1
                    if old_seen != old_count or new_seen != new_count:
                        raise ValueError("Malformed patch hunk")
                    hunk_count += 1
                if not hunk_count:
                    raise ValueError("Malformed patch")
                output.extend(source_lines[cursor:])
                planned[target] = "\n".join(output) + ("\n" if source.endswith("\n") else "")
        except (OSError, ValueError) as exc:
            return ExecutionResult("failed", error_summary=str(exc)[:200])
        # Every target and hunk has been checked before any write; staged temp files avoid partial validation application.
        staged: list[tuple[Path, str]] = []
        try:
            for target, content in planned.items():
                descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(content)
                staged.append((target, temporary))
            originals = {target: target.read_text(encoding="utf-8", errors="replace") for target in planned}
            applied: list[Path] = []
            try:
                for target, temporary in staged:
                    os.replace(temporary, target)
                    applied.append(target)
            except OSError:
                for target in applied:
                    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.rollback.", dir=target.parent)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                        output.write(originals[target])
                    os.replace(temporary, target)
                raise
        except OSError:
            for _, temporary in staged:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return ExecutionResult("failed", error_summary="Patch application failed")
        return ExecutionResult("succeeded", {"files_changed": len(planned), "check_first": bool(request.payload.get("check_first", True))})

    async def _run_tests(self, request: ExecutionRequest, environment: dict) -> ExecutionResult:
        workspace = self._workspace(environment)
        target = request.payload.get("target")
        if workspace is None or not workspace.is_dir():
            return ExecutionResult("failed", error_summary="Environment workspace is unavailable")
        command = self.test_targets.get(target)
        if command is None:
            return ExecutionResult("failed", error_summary="Unsupported test target")
        timeout = request.timeout_seconds if request.timeout_seconds is not None else 30.0
        if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 120:
            return ExecutionResult("failed", error_summary="Invalid test timeout")
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(*command, cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), float(timeout))
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return ExecutionResult("failed", error_summary="Test execution timed out", duration_ms=(time.perf_counter() - started) * 1000)
        return ExecutionResult(
            "succeeded" if process.returncode == 0 else "failed",
            {"target": target, "exit_code": process.returncode, "stdout": stdout[:self.max_output_bytes].decode(errors="replace"), "stderr": stderr[:self.max_output_bytes].decode(errors="replace"), "stdout_truncated": len(stdout) > self.max_output_bytes, "stderr_truncated": len(stderr) > self.max_output_bytes},
            duration_ms=(time.perf_counter() - started) * 1000,
        )
