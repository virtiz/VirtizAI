from __future__ import annotations

from dataclasses import dataclass


VERBOSITIES = {"minimal", "concise", "normal", "detailed", "expert"}
EXECUTION_UPDATES = {"silent", "important_milestones", "detailed", "full_trace"}
TOOL_DETAILS = {"hidden", "summary", "commands_results"}


@dataclass(frozen=True)
class CommunicationPolicy:
    response_verbosity: str = "normal"
    execution_updates: str = "important_milestones"
    tool_details: str = "summary"

    def output_token_budget(self, default: int = 8192) -> int:
        return {"minimal": min(default, 512), "concise": min(default, 1024), "normal": min(default, 4096), "detailed": min(default, 8192), "expert": default}[self.response_verbosity]

    def should_surface(self, event: str) -> bool:
        if self.execution_updates == "silent": return False
        if self.execution_updates == "full_trace": return True
        if self.execution_updates == "detailed": return event in {"started", "important_discovery", "needs_input", "fallback", "failed", "completed"}
        return event in {"started", "needs_input", "fallback", "failed", "completed"}


def normalize_policy(response: str | None, updates: str | None, tools: str | None, base: CommunicationPolicy | None = None) -> CommunicationPolicy:
    current = base or CommunicationPolicy()
    return CommunicationPolicy(response if response in VERBOSITIES else current.response_verbosity, updates if updates in EXECUTION_UPDATES else current.execution_updates, tools if tools in TOOL_DETAILS else current.tool_details)
