"""Deterministic capability requirements for provider/model route selection."""
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any

REQUIRED_STATES = {"verified", "probed"}

@dataclass(frozen=True)
class TaskRequirements:
    required_capabilities: tuple[str, ...]
    preferred_capabilities: tuple[str, ...] = ()
    prefer_local: bool = False
    latency_preference: str = "balanced"
    cost_preference: str = "balanced"
    execution_capabilities: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"required_capabilities": list(self.required_capabilities), "preferred_capabilities": list(self.preferred_capabilities), "execution_capabilities_any": list(self.execution_capabilities), "prefer_local": self.prefer_local, "latency_preference": self.latency_preference, "cost_preference": self.cost_preference}

ROLE_REQUIREMENTS = {
    "role-secretary": TaskRequirements(("chat",)),
    # Coding may use either a native-tool model or a bounded managed worker.
    "role-coding": TaskRequirements(("coding",), ("local",), True, execution_capabilities=("native_tool_calls", "managed_coding_worker")),
    "role-infrastructure": TaskRequirements(("chat", "native_tool_calls")),
    "role-project-lead": TaskRequirements(("chat", "native_tool_calls", "structured_output"), ("reasoning",)),
}

def requirements_for(role_id: str) -> TaskRequirements:
    return ROLE_REQUIREMENTS.get(role_id, TaskRequirements(("chat",)))

def capability_state(row: dict, capability: str) -> str:
    """Configured evidence overrides advertised capabilities; never infer names."""
    try: advertised = json.loads(row.get("capabilities_json") or "[]")
    except (TypeError, json.JSONDecodeError): advertised = []
    try: overrides = json.loads(row.get("user_overrides_json") or "{}")
    except (TypeError, json.JSONDecodeError): overrides = {}
    evidence = overrides.get("capability_evidence", {}) if isinstance(overrides, dict) else {}
    state = evidence.get(capability) if isinstance(evidence, dict) else None
    if state in {"advertised", "configured", "probed", "verified", "failed", "unsupported"}: return state
    if isinstance(advertised, list) and capability in advertised: return "advertised"
    if isinstance(advertised, dict) and advertised.get(capability): return "advertised"
    return "unsupported"
