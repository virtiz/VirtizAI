"""Generic bounded infrastructure execution with optional adapters."""
from __future__ import annotations
import asyncio, json, time, urllib.parse, urllib.request
from typing import Any
from .infra_policy import authorize, cluster_read_authorized, operation_policy, resource_scope
from .workers import ExecutionRequest, ExecutionResult

class InfrastructureToolsExecutor:
    worker_type = "infrastructure"
    operations = {"inspect_host", "list_vms", "inspect_vm", "inspect_service", "start_vm", "restart_vm"}
    task_poll_attempts = 12
    task_poll_seconds = 1.0

    @staticmethod
    def _config(environment: dict) -> dict[str, Any]:
        try: value = json.loads(environment.get("config_json") or "{}")
        except json.JSONDecodeError: value = {}
        return value if isinstance(value, dict) else {}

    def __init__(self, secret_store=None): self.secret_store = secret_store
    @staticmethod
    def _error(code: str) -> ExecutionResult: return ExecutionResult("failed", error_summary=code)

    @staticmethod
    def _evidence(result: ExecutionResult, risk: str, authorization_source: str, adapter: str) -> ExecutionResult:
        output = dict(result.output) if isinstance(result.output, dict) else {}
        output.update({"risk_class": risk, "authorization_source": authorization_source, "adapter": adapter})
        return ExecutionResult(result.status, output, result.error_summary, result.duration_ms)

    def _request(self, endpoint: str, token_id: str, token: str, path: str, method: str = "GET") -> Any:
        request = urllib.request.Request(endpoint.rstrip("/")+path, headers={"Authorization":"PVEAPIToken="+token_id+"="+token,"Accept":"application/json"}, method=method)
        with urllib.request.urlopen(request, timeout=8) as response: return json.loads(response.read(100000)).get("data")

    @staticmethod
    def _normalize_vm(item: dict) -> dict[str, Any]:
        return {"id":str(item.get("vmid")),"name":str(item.get("name", ""))[:160],"state":item.get("status"),"host":item.get("node"),"resource_type":str(item.get("type") or "vm"),"cpu":item.get("maxcpu"),"memory":item.get("maxmem")}

    def _proxmox_vms(self, endpoint: str, token_id: str, token: str, config: dict, allowed: list[str], cluster_read: bool = False) -> list[dict[str, Any]]:
        node = config.get("proxmox_node")
        if isinstance(node, str) and node:
            safe_node = urllib.parse.quote(node, safe="")
            values = []
            for vm_id in allowed:
                data = self._request(endpoint, token_id, token, f"/api2/json/nodes/{safe_node}/qemu/{urllib.parse.quote(vm_id, safe='')}/status/current")
                if isinstance(data, dict):
                    values.append(self._normalize_vm({**data, "vmid": vm_id, "node": node}))
            return values
        data = self._request(endpoint, token_id, token, "/api2/json/cluster/resources?type=vm")
        return [self._normalize_vm(item) for item in (data if isinstance(data, list) else []) if isinstance(item, dict) and (cluster_read or str(item.get("vmid")) in allowed)][:50]

    def _proxmox(self, request: ExecutionRequest, environment: dict, config: dict) -> ExecutionResult:
        reference, endpoint = environment.get("credential_ref"), environment.get("address")
        if not isinstance(reference,str) or not reference or self.secret_store is None or not self.secret_store.configured(reference): return self._error("credential_unavailable")
        if not isinstance(endpoint,str) or not endpoint.startswith("https://"): return self._error("endpoint_invalid")
        token_id = config.get("api_token_id")
        if not isinstance(token_id,str) or not token_id: return self._error("credential_unavailable")
        try:
            token=self.secret_store.get(reference); allowed=resource_scope(config, request.operation)
            cluster_read = cluster_read_authorized(config, request.operation)
            normalized=self._proxmox_vms(endpoint, token_id, token, config, allowed, cluster_read)
            if request.operation == "inspect_host":
                host_id=request.payload.get("host_id"); data=self._request(endpoint,token_id,token,f"/api2/json/nodes/{urllib.parse.quote(str(host_id),safe='')}/status")
                return ExecutionResult("succeeded",{"id":host_id,"platform":"proxmox","state":"online"} if isinstance(data,dict) else {},None if isinstance(data,dict) else "adapter_invalid_data")
            if request.operation == "list_vms": return ExecutionResult("succeeded", {"resources":normalized})
            vm_id=request.payload.get("vm_id"); found=next((item for item in normalized if item["id"]==vm_id),None)
            if request.operation == "inspect_vm": return ExecutionResult("succeeded",found or {},None if found else "resource_not_found")
            if found is None: return self._error("resource_not_found")
            if request.operation == "start_vm" and found.get("state")=="running": return ExecutionResult("succeeded",{"resource_id":vm_id,"operation":"start_vm","accepted":False,"pre_state":"running","state":"running","outcome":"already_running"})
            if request.operation == "restart_vm" and found.get("state")!="running": return self._error("invalid_resource_state")
            action="start" if request.operation=="start_vm" else "reboot"; node=urllib.parse.quote(str(found["host"]),safe="")
            task_id=self._request(endpoint,token_id,token,f"/api2/json/nodes/{node}/qemu/{urllib.parse.quote(str(vm_id),safe='')}/status/{action}","POST")
            if not isinstance(task_id,str) or not task_id: return self._error("backend_operation_failed")
            completed=False; task_path=urllib.parse.quote(task_id,safe="")
            for _ in range(self.task_poll_attempts):
                status=self._request(endpoint,token_id,token,f"/api2/json/nodes/{node}/tasks/{task_path}/status")
                if isinstance(status,dict) and status.get("status")=="stopped":
                    if status.get("exitstatus")!="OK": return self._error("backend_operation_failed")
                    completed=True; break
                time.sleep(self.task_poll_seconds)
            if not completed: return self._error("postcondition_timeout")
            check = None
            for _ in range(self.task_poll_attempts):
                check=next((item for item in self._proxmox_vms(endpoint, token_id, token, config, [vm_id]) if item["id"] == vm_id), None)
                if check is not None and check.get("state") == "running": break
                time.sleep(self.task_poll_seconds)
            if check is None or check.get("state")!="running": return self._error("postcondition_timeout")
            return ExecutionResult("succeeded",{"resource_id":vm_id,"operation":request.operation,"accepted":True,"task_id":task_id[:240],"pre_state":found.get("state"),"state":check.get("state"),"host":check.get("host"),"outcome":"verified"})
        except Exception: return self._error("backend_operation_failed")

    async def execute(self, request: ExecutionRequest, worker: dict, environment: dict) -> ExecutionResult:
        policy=operation_policy(request.operation)
        if request.operation not in self.operations: return self._error("operation_not_allowed" if policy is None else "destructive_operation_disabled")
        try: worker_caps=set(json.loads(worker.get("capabilities_json") or "[]")); environment_caps=set(json.loads(environment.get("capabilities_json") or "[]"))
        except json.JSONDecodeError: return self._error("capability_missing")
        if "read_infrastructure" not in worker_caps or "read_infrastructure" not in environment_caps: return self._error("capability_missing")
        config=self._config(environment); decision=authorize(request.operation,worker_caps,environment_caps,config)
        if not decision.allowed: return self._error(decision.code or "operation_not_allowed")
        allowed=resource_scope(config, request.operation)
        cluster_read = cluster_read_authorized(config, request.operation)
        if request.operation in {"inspect_vm","start_vm","restart_vm"} and request.payload.get("vm_id") not in allowed and not (request.operation == "inspect_vm" and cluster_read): return self._error("resource_out_of_scope")
        if config.get("adapter")=="proxmox": return self._evidence(await asyncio.to_thread(self._proxmox,request,environment,config), decision.risk.value, decision.authorization_source or "", "proxmox")
        if cluster_read: return self._error("adapter_not_configured")
        inventory=config.get("inventory")
        if not isinstance(inventory,dict): return self._error("adapter_not_configured")
        if not isinstance(allowed,list) or not all(isinstance(item,str) for item in allowed): return self._error("resource_out_of_scope")
        hosts,vms,services=inventory.get("hosts",[]),inventory.get("vms",[]),inventory.get("services",[])
        if not all(isinstance(group,list) and all(isinstance(item,dict) for item in group) for group in (hosts,vms,services)): return self._error("adapter_invalid_data")
        if request.operation=="inspect_host":
            found=next((item for item in hosts if item.get("id")==request.payload.get("host_id")),None); return ExecutionResult("succeeded",{key:found.get(key) for key in ("id","platform","state","cpu","memory")} if found else {},None if found else "resource_out_of_scope")
        if request.operation=="list_vms": return ExecutionResult("succeeded",{"resources":[{key:item.get(key) for key in ("id","name","state","resource_type","host")} for item in vms[:50] if item.get("id") in allowed]})
        if request.operation=="inspect_vm":
            found=next((item for item in vms if item.get("id")==request.payload.get("vm_id")),None); return ExecutionResult("succeeded",{key:found.get(key) for key in ("id","name","state","cpu","memory","host","resource_type")} if found else {},None if found else "resource_not_found")
        if request.operation in {"start_vm","restart_vm"}: return self._error("adapter_not_configured")
        found=next((item for item in services if item.get("id")==request.payload.get("service_id")),None); return ExecutionResult("succeeded",{key:found.get(key) for key in ("id","active","processes","restarts","health")} if found else {},None if found else "resource_out_of_scope")
