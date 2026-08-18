from __future__ import annotations

import asyncio
import json
import resource
import signal
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Protocol

from .db import Database


@dataclass(frozen=True)
class ExecutionPolicy:
    timeout_seconds: float = 10
    max_output_bytes: int = 16_384
    max_processes: int = 32
    max_open_files: int = 128
    max_workspace_bytes: int = 50_000_000
    max_concurrency: int = 2
    max_memory_bytes: int | None = None


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    target: str
    tool: str
    driver: str
    artifacts: list[str]
    truncated: bool = False
    timeout: bool = False
    resource_limit: bool = False
    error_classification: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class ExecutionDriver(Protocol):
    name: str
    capabilities: dict[str, bool | str]
    async def execute(self, argv: list[str], workspace: Path, policy: ExecutionPolicy, target: str, tool: str) -> ExecutionResult: ...


class LocalExecutionDriver:
    name = "local"
    capabilities = {"memory_isolation": False, "memory_limit": "rlimit_as", "container_grade": False}

    @staticmethod
    def _limits(policy: ExecutionPolicy) -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (max(1, int(policy.timeout_seconds)), max(1, int(policy.timeout_seconds))))
            resource.setrlimit(resource.RLIMIT_NPROC, (policy.max_processes, policy.max_processes))
            resource.setrlimit(resource.RLIMIT_NOFILE, (policy.max_open_files, policy.max_open_files))
            if policy.max_memory_bytes is not None:
                resource.setrlimit(resource.RLIMIT_AS, (policy.max_memory_bytes, policy.max_memory_bytes))
        except (ValueError, OSError):
            pass

    async def execute(self, argv: list[str], workspace: Path, policy: ExecutionPolicy, target: str, tool: str) -> ExecutionResult:
        started = monotonic()
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(*argv, cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, preexec_fn=lambda: self._limits(policy))
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), policy.timeout_seconds)
                timed_out = False
            except asyncio.TimeoutError:
                process.send_signal(signal.SIGKILL)
                stdout, stderr = await process.communicate()
                timed_out = True
            combined = stdout + stderr
            truncated = len(combined) > policy.max_output_bytes
            output = combined[:policy.max_output_bytes]
            status = "TIMEOUT" if timed_out else ("SUCCESS" if process.returncode == 0 else "FAILED")
            return ExecutionResult(status, process.returncode, output.decode(errors="replace"), "" if not timed_out else "execution timed out", (monotonic() - started) * 1000, target, tool, self.name, [], truncated, timed_out, False, None if status == "SUCCESS" else "process_error")
        except PermissionError as exc:
            return ExecutionResult("PERMISSION_DENIED", None, "", str(exc), (monotonic() - started) * 1000, target, tool, self.name, [], error_classification="permission")
        except OSError as exc:
            return ExecutionResult("FAILED", None, "", str(exc), (monotonic() - started) * 1000, target, tool, self.name, [], error_classification="os_error")


class SandboxExecutionDriver(LocalExecutionDriver):
    name = "sandbox"
    capabilities = {"memory_isolation": False, "memory_limit": "not_configured", "container_grade": False}


class RemoteExecutionDriver:
    name = "remote"
    capabilities = {"memory_isolation": False, "memory_limit": "remote_unknown", "container_grade": False}
    async def execute(self, argv: list[str], workspace: Path, policy: ExecutionPolicy, target: str, tool: str) -> ExecutionResult:
        return ExecutionResult("ESCALATE", None, "", "Remote driver is optional and not configured", 0, target, tool, self.name, [], error_classification="not_configured")


class ExecutionManager:
    def __init__(self, database: Database, workspace_dir: Path) -> None:
        self.database = database
        self.workspace_dir = workspace_dir
        self.drivers: dict[str, ExecutionDriver] = {"local": LocalExecutionDriver(), "sandbox": SandboxExecutionDriver(), "remote": RemoteExecutionDriver()}
        self.semaphore = asyncio.Semaphore(2)
        self.active: dict[str, asyncio.Task[ExecutionResult]] = {}

    def allocate_workspace(self, job_id: str) -> Path:
        path = self.workspace_dir / "jobs" / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def create_attempt(self, job_id: str, tool_id: str | None = None, environment_target_id: str | None = None) -> str:
        attempt_id = str(uuid.uuid4())
        self.database.execute("INSERT INTO execution_attempts(id, job_id, tool_id, environment_target_id, driver) VALUES (?, ?, ?, ?, 'local')", (attempt_id, job_id, tool_id, environment_target_id))
        return attempt_id

    async def run(self, job_id: str, tool_name: str, argv: list[str], policy: ExecutionPolicy | None = None, target: str = "local") -> ExecutionResult:
        policy = policy or ExecutionPolicy()
        workspace = self.allocate_workspace(job_id)
        async with self.semaphore:
            task = asyncio.create_task(self.drivers["local"].execute(argv, workspace, policy, target, tool_name))
            self.active[job_id] = task
            try:
                return await task
            finally:
                self.active.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        task = self.active.get(job_id)
        return bool(task and task.cancel())

    def driver_capabilities(self) -> dict[str, dict[str, bool | str]]:
        return {name: getattr(driver, "capabilities", {}) for name, driver in self.drivers.items()}

    def cleanup_workspace(self, job_id: str) -> None:
        path = self.workspace_dir / "jobs" / job_id
        if path.exists():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink(): child.unlink(missing_ok=True)
                elif child.is_dir(): child.rmdir()
            path.rmdir()

    def record_audit(self, user_id: str | None, job_id: str, tool_id: str | None, result: ExecutionResult, args: dict, authorization: str = "allowed") -> None:
        sanitized = {key: "<redacted>" if any(word in key.lower() for word in ("secret", "password", "token")) else value for key, value in args.items()}
        self.database.execute("INSERT INTO execution_audit(user_id, job_id, tool_id, driver, sanitized_args_json, authorization, result_json) VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, job_id, tool_id, result.driver, json.dumps(sanitized), authorization, json.dumps(result.as_dict())))
