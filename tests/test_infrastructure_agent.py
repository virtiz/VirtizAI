import asyncio, json
from pathlib import Path
from virtizai_core.db import Database
from virtizai_core.infra_tools import InfrastructureToolsExecutor
from virtizai_core.workers import ExecutionRequest, WorkerExecutionBoundary, WorkerExecutionError

def setup(tmp_path, caps=("read_infrastructure","inspect_vm","list_vms","inspect_host","inspect_service")):
 db=Database(tmp_path/'i.db');db.open();db.execute("INSERT INTO workers(id,name,worker_type,status,capabilities_json,config_json) VALUES('w','I','infrastructure','available',?, '{}')",(json.dumps(list(caps)),)); cfg={"allowed_worker_types":["infrastructure"],"allowed_resource_ids":["120"],"inventory":{"hosts":[{"id":"node-a","platform":"proxmox","state":"online"}],"vms":[{"id":"120","name":"safe","state":"running","host":"node-a","resource_type":"vm"}],"services":[{"id":"virtizai","active":True,"processes":1,"restarts":0,"health":"ok"}]}};db.execute("INSERT INTO environment_targets(id,name,target_type,enabled,status,capabilities_json,config_json) VALUES('e','E','infrastructure',1,'available',?,?)",(json.dumps(list(caps)),json.dumps(cfg)));b=WorkerExecutionBoundary(db);b.register(InfrastructureToolsExecutor());return db,b
def test_typed_infra_operations_and_scope(tmp_path):
 db,b=setup(tmp_path); r=asyncio.run(b.execute(ExecutionRequest('w','e','inspect_vm',{'vm_id':'120'})));assert r.status=='succeeded' and r.output['state']=='running'; assert asyncio.run(b.execute(ExecutionRequest('w','e','list_vms',{}))).status=='succeeded';assert asyncio.run(b.execute(ExecutionRequest('w','e','inspect_service',{'service_id':'virtizai'}))).status=='succeeded'; assert asyncio.run(b.execute(ExecutionRequest('w','e','inspect_vm',{'vm_id':'121'}))).status=='failed'; assert asyncio.run(b.execute(ExecutionRequest('w','e','command',{'command':'id'}))).status=='failed';db.close()
def test_capability_and_disabled_rejected(tmp_path):
 db,b=setup(tmp_path,("read_infrastructure",));assert asyncio.run(b.execute(ExecutionRequest('w','e','inspect_vm',{'vm_id':'120'}))).status=='failed';db.execute("UPDATE workers SET enabled=0 WHERE id='w'");
 try: asyncio.run(b.execute(ExecutionRequest('w','e','list_vms',{})));assert False
 except WorkerExecutionError: pass
 db.close()
