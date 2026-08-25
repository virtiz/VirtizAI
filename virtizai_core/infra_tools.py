"""Generic typed read-only infrastructure executor.

Adapters receive structured inventory/configuration only; no model supplied
command, shell, or transport syntax is accepted.
"""
from __future__ import annotations
import json
from typing import Any
from .workers import ExecutionRequest, ExecutionResult

class InfrastructureToolsExecutor:
    worker_type = "infrastructure"
    operations = {"inspect_host", "list_vms", "inspect_vm", "inspect_service"}

    @staticmethod
    def _config(environment: dict) -> dict[str, Any]:
        try: value = json.loads(environment.get("config_json") or "{}")
        except json.JSONDecodeError: value = {}
        return value if isinstance(value, dict) else {}

    async def execute(self, request: ExecutionRequest, worker: dict, environment: dict) -> ExecutionResult:
        if request.operation not in self.operations:
            return ExecutionResult("failed", error_summary="Unsupported infrastructure operation")
        caps = set(json.loads(worker.get("capabilities_json") or "[]")) & set(json.loads(environment.get("capabilities_json") or "[]"))
        if request.operation not in caps or "read_infrastructure" not in caps:
            return ExecutionResult("failed", error_summary="Infrastructure capability is not allowed")
        config = self._config(environment); inventory = config.get("inventory")
        if not isinstance(inventory, dict):
            return ExecutionResult("failed", error_summary="Infrastructure adapter is not configured")
        allowed = config.get("allowed_resource_ids", [])
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            return ExecutionResult("failed", error_summary="Infrastructure resource scope is invalid")
        hosts = inventory.get("hosts", []); vms = inventory.get("vms", []); services = inventory.get("services", [])
        if not all(isinstance(group, list) and all(isinstance(item, dict) for item in group) for group in (hosts, vms, services)):
            return ExecutionResult("failed", error_summary="Infrastructure adapter inventory is invalid")
        if request.operation == "inspect_host":
            host_id = request.payload.get("host_id")
            if not isinstance(host_id, str): return ExecutionResult("failed", error_summary="Invalid host identifier")
            found = next((x for x in hosts if x.get("id") == host_id), None)
            return ExecutionResult("succeeded", {k: found.get(k) for k in ("id", "platform", "state", "cpu", "memory")} if found else {}, None if found else "Host is outside approved scope")
        if request.operation == "list_vms":
            return ExecutionResult("succeeded", {"resources": [{k:x.get(k) for k in ("id","name","state","resource_type","host")} for x in vms[:50] if not allowed or x.get("id") in allowed]})
        if request.operation == "inspect_vm":
            vm_id = request.payload.get("vm_id")
            if not isinstance(vm_id, str) or vm_id not in allowed: return ExecutionResult("failed", error_summary="VM is outside approved scope")
            found = next((x for x in vms if x.get("id") == vm_id), None)
            return ExecutionResult("succeeded", {k: found.get(k) for k in ("id","name","state","cpu","memory","host","resource_type")} if found else {}, None if found else "VM not found")
        service_id = request.payload.get("service_id")
        if not isinstance(service_id, str): return ExecutionResult("failed", error_summary="Invalid service identifier")
        found = next((x for x in services if x.get("id") == service_id), None)
        return ExecutionResult("succeeded", {k: found.get(k) for k in ("id","active","processes","restarts","health")} if found else {}, None if found else "Service is outside approved scope")
