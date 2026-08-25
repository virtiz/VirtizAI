"""Generic typed read-only infrastructure executor.

Adapters receive structured inventory/configuration only; no model supplied
command, shell, or transport syntax is accepted.
"""
from __future__ import annotations
import json
import urllib.request
import asyncio
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

    def __init__(self, secret_store=None): self.secret_store = secret_store

    def _proxmox(self, request: ExecutionRequest, environment: dict, config: dict) -> ExecutionResult:
        reference = environment.get("credential_ref")
        endpoint = environment.get("address")
        if not isinstance(reference, str) or not reference or self.secret_store is None or not self.secret_store.configured(reference): return ExecutionResult("failed", error_summary="Infrastructure credential reference is unavailable")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"): return ExecutionResult("failed", error_summary="Infrastructure endpoint is invalid")
        token = self.secret_store.get(reference)
        token_id = config.get("api_token_id")
        if not isinstance(token_id, str) or not token_id:
            return ExecutionResult("failed", error_summary="Infrastructure API token identity is unavailable")
        paths = {"inspect_host": f"/api2/json/nodes/{request.payload.get('host_id')}/status", "list_vms": "/api2/json/cluster/resources?type=vm", "inspect_vm": f"/api2/json/cluster/resources?type=vm", "inspect_service": ""}
        path = paths.get(request.operation, "")
        if not path: return ExecutionResult("failed", error_summary="Operation is not mapped for this infrastructure adapter")
        try:
            req=urllib.request.Request(endpoint.rstrip("/")+path, headers={"Authorization": "PVEAPIToken="+token_id+"="+token, "Accept":"application/json"})
            with urllib.request.urlopen(req, timeout=8) as response: data=json.loads(response.read(100000)).get("data")
        except Exception: return ExecutionResult("failed", error_summary="Infrastructure adapter request failed")
        allowed=config.get("allowed_resource_ids", [])
        if request.operation == "inspect_host":
            return ExecutionResult("succeeded", {"id":request.payload["host_id"],"platform":"proxmox","state":"online"} if isinstance(data,dict) else {}, None if isinstance(data,dict) else "Infrastructure adapter returned invalid data")
        items=data if isinstance(data,list) else []
        normalized=[{"id":str(x.get("vmid")),"name":str(x.get("name", ""))[:160],"state":x.get("status"),"host":x.get("node"),"resource_type":"vm","cpu":x.get("maxcpu"),"memory":x.get("maxmem")} for x in items if isinstance(x,dict) and (not allowed or str(x.get("vmid")) in allowed)][:50]
        if request.operation == "list_vms": return ExecutionResult("succeeded", {"resources":normalized})
        found=next((x for x in normalized if x["id"]==request.payload["vm_id"]),None)
        return ExecutionResult("succeeded", found or {}, None if found else "VM not found")

    async def execute(self, request: ExecutionRequest, worker: dict, environment: dict) -> ExecutionResult:
        if request.operation not in self.operations:
            return ExecutionResult("failed", error_summary="Unsupported infrastructure operation")
        caps = set(json.loads(worker.get("capabilities_json") or "[]")) & set(json.loads(environment.get("capabilities_json") or "[]"))
        if request.operation not in caps or "read_infrastructure" not in caps:
            return ExecutionResult("failed", error_summary="Infrastructure capability is not allowed")
        config = self._config(environment); inventory = config.get("inventory")
        allowed = config.get("allowed_resource_ids", [])
        if request.operation == "inspect_vm" and (not isinstance(allowed, list) or request.payload.get("vm_id") not in allowed):
            return ExecutionResult("failed", error_summary="VM is outside approved scope")
        if config.get("adapter") == "proxmox":
            return await asyncio.to_thread(self._proxmox, request, environment, config)
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
