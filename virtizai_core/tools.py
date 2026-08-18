from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from .execution import ExecutionManager, ExecutionPolicy, ExecutionResult


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    display_name: str
    description: str
    argument_schema: dict[str, Any]
    required_permissions: tuple[str, ...]
    allowed_drivers: tuple[str, ...]
    timeout_seconds: float = 10
    max_output_bytes: int = 16_384


class ToolAuthorizationError(PermissionError):
    pass


class ToolRegistryService:
    def __init__(self, execution: ExecutionManager) -> None:
        self.execution = execution
        self.tools: dict[str, ToolDefinition] = {
            "host_status": ToolDefinition("host_status", "Host status", "Read safe local host status", {"type": "object", "additionalProperties": False}, ("read:host",), ("local",), 5, 8_192),
            "file_read": ToolDefinition("file_read", "Read workspace file", "Read a file inside the current job workspace", {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}, ("read:workspace",), ("local", "sandbox"), 5, 16_384),
            "sleep": ToolDefinition("sleep", "Sleep test", "Synthetic timeout/cancellation test tool", {"type": "object", "required": ["seconds"], "properties": {"seconds": {"type": "number", "minimum": 0, "maximum": 3600}}}, ("test:execution",), ("local", "sandbox"), 10, 1_024),
        }
        self.permission_profiles = {
            "secretary": {"read:host"},
            "general": {"read:host", "read:workspace", "test:execution"},
            "coding": {"read:host", "read:workspace", "test:execution"},
            "admin": {"read:host", "read:workspace", "test:execution"},
        }

    def list_tools(self) -> list[dict]:
        return [{"name": tool.name, "display_name": tool.display_name, "description": tool.description, "required_permissions": tool.required_permissions, "allowed_drivers": tool.allowed_drivers} for tool in self.tools.values()]

    def authorize(self, tool_name: str, profile: str) -> ToolDefinition:
        tool = self.tools.get(tool_name)
        if tool is None:
            raise LookupError(f"Unknown tool: {tool_name}")
        if not set(tool.required_permissions).issubset(self.permission_profiles.get(profile, set())):
            raise ToolAuthorizationError(f"Profile {profile} is not authorized for {tool_name}")
        return tool

    async def run(self, job_id: str, tool_name: str, args: dict, profile: str = "secretary") -> ExecutionResult:
        tool = self.authorize(tool_name, profile)
        if tool_name == "host_status":
            argv = ["/bin/sh", "-c", "printf 'status=ok\\n'; uname -srm"]
        elif tool_name == "file_read":
            path = args.get("path", "")
            if path.startswith("/") or ".." in path.split("/"):
                raise ToolAuthorizationError("Path must remain inside the job workspace")
            argv = ["/bin/cat", "--", path]
        elif tool_name == "sleep":
            seconds = float(args.get("seconds", 0))
            if seconds < 0 or seconds > 3600:
                raise ValueError("seconds outside allowed range")
            argv = ["/bin/sleep", str(seconds)]
        else:
            raise LookupError(tool_name)
        return await self.execution.run(job_id, tool.name, argv, ExecutionPolicy(tool.timeout_seconds, tool.max_output_bytes))
