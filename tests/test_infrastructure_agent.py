import asyncio, json
from virtizai_core.db import Database
from virtizai_core.infra_policy import InfrastructureRisk, authorize, operation_policy
from virtizai_core.infra_tools import InfrastructureToolsExecutor
from virtizai_core.workers import ExecutionRequest, WorkerExecutionBoundary, WorkerExecutionError

READ=("read_infrastructure","inspect_vm","list_vms","inspect_host","inspect_service")
MUTATING=READ+("start_vm","restart_vm")

def setup(tmp_path, worker_caps=READ, environment_caps=READ, config=None):
 db=Database(tmp_path/'i.db');db.open();db.execute("INSERT INTO workers(id,name,worker_type,status,capabilities_json,config_json) VALUES('w','I','infrastructure','available',?, '{}')",(json.dumps(list(worker_caps)),)); cfg={"allowed_worker_types":["infrastructure"],"allowed_resource_ids":["120"],"inventory":{"hosts":[{"id":"node-a","platform":"generic","state":"online"}],"vms":[{"id":"120","name":"safe","state":"running","host":"node-a","resource_type":"vm"}],"services":[{"id":"virtizai","active":True,"processes":1,"restarts":0,"health":"ok"}]}};cfg.update(config or {});db.execute("INSERT INTO environment_targets(id,name,target_type,enabled,status,capabilities_json,config_json) VALUES('e','E','infrastructure',1,'available',?,?)",(json.dumps(list(environment_caps)),json.dumps(cfg)));b=WorkerExecutionBoundary(db);b.register(InfrastructureToolsExecutor());return db,b

def run(boundary, operation, payload): return asyncio.run(boundary.execute(ExecutionRequest('w','e',operation,payload)))

def test_typed_read_operations_and_scope(tmp_path):
 db,b=setup(tmp_path);assert run(b,'inspect_vm',{'vm_id':'120'}).output['state']=='running';assert run(b,'list_vms',{}).status=='succeeded';assert run(b,'inspect_service',{'service_id':'virtizai'}).status=='succeeded';assert run(b,'inspect_vm',{'vm_id':'121'}).error_summary=='resource_out_of_scope';assert run(b,'command',{'command':'id'}).error_summary=='operation_not_allowed';db.close()

def test_risk_taxonomy_is_centralized():
 assert operation_policy('inspect_vm').risk is InfrastructureRisk.READ
 assert operation_policy('start_vm').risk is InfrastructureRisk.MUTATING_REVERSIBLE
 assert operation_policy('restart_vm').risk is InfrastructureRisk.MUTATING_DISRUPTIVE
 assert authorize('delete_vm',set(),set(),{}).code=='destructive_operation_disabled'

def test_mutation_requires_capability_and_environment_policy(tmp_path):
 db,b=setup(tmp_path,MUTATING,MUTATING);assert run(b,'restart_vm',{'vm_id':'120'}).error_summary=='risk_not_authorized';db.close()
 db,b=setup(tmp_path/'reversible',MUTATING,MUTATING,{"allowed_risk_classes":["MUTATING_REVERSIBLE"]});assert run(b,'start_vm',{'vm_id':'120'}).error_summary=='adapter_not_configured';assert run(b,'restart_vm',{'vm_id':'120'}).error_summary=='risk_not_authorized';db.close()
 db,b=setup(tmp_path/'missing',READ,MUTATING,{"allowed_risk_classes":["MUTATING_REVERSIBLE","MUTATING_DISRUPTIVE"],"preauthorized_operations":["restart_vm"]});assert run(b,'restart_vm',{'vm_id':'120'}).error_summary=='capability_missing';db.close()

def test_scope_and_disabled_worker_remain_authoritative(tmp_path):
 db,b=setup(tmp_path);assert run(b,'inspect_vm',{'vm_id':'999'}).error_summary=='resource_out_of_scope';db.execute("UPDATE workers SET enabled=0 WHERE id='w'");
 try: run(b,'list_vms',{});assert False
 except WorkerExecutionError: pass
 db.close()

def test_proxmox_mutation_polls_then_verifies(tmp_path, monkeypatch):
 class Secrets:
  def configured(self, ref): return ref=='ref'
  def get(self, ref): return 'never-exposed'
 db=Database(tmp_path/'p.db');db.open();cfg={"adapter":"proxmox","api_token_id":"user@pve!token","allowed_worker_types":["infrastructure"],"allowed_resource_ids":["120"],"allowed_risk_classes":["MUTATING_REVERSIBLE","MUTATING_DISRUPTIVE"],"preauthorized_operations":["restart_vm"]};db.execute("INSERT INTO workers(id,name,worker_type,status,capabilities_json,config_json) VALUES('w','I','infrastructure','available',?, '{}')",(json.dumps(MUTATING),));db.execute("INSERT INTO environment_targets(id,name,target_type,address,credential_ref,enabled,status,capabilities_json,config_json) VALUES('e','E','infrastructure','https://unit.invalid','ref',1,'available',?,?)",(json.dumps(MUTATING),json.dumps(cfg)));executor=InfrastructureToolsExecutor(Secrets());calls=[]
 def request(endpoint, token_id, token, path, method='GET'):
  calls.append((method,path))
  if path.startswith('/api2/json/cluster/resources'): return [{"vmid":120,"name":"safe","status":"running","node":"node-a","maxcpu":2,"maxmem":1024}]
  if path.endswith('/status/reboot'): return 'UPID:unit'
  if '/tasks/' in path: return {"status":"stopped","exitstatus":"OK"}
  raise AssertionError(path)
 monkeypatch.setattr(executor,'_request',request);monkeypatch.setattr('virtizai_core.infra_tools.time.sleep',lambda _:None);b=WorkerExecutionBoundary(db);b.register(executor);result=run(b,'restart_vm',{'vm_id':'120'});assert result.status=='succeeded';assert result.output['state']=='running' and result.output['task_id']=='UPID:unit';assert result.output['risk_class']=='MUTATING_DISRUPTIVE' and result.output['authorization_source']=='environment_preauthorization';assert any(method=='POST' for method,path in calls);db.close()

def test_proxmox_timeout_never_reports_success(tmp_path, monkeypatch):
 class Secrets:
  def configured(self, ref): return True
  def get(self, ref): return 'secret'
 db=Database(tmp_path/'t.db');db.open();cfg={"adapter":"proxmox","api_token_id":"user@pve!token","allowed_worker_types":["infrastructure"],"allowed_resource_ids":["120"],"allowed_risk_classes":["MUTATING_REVERSIBLE"]};db.execute("INSERT INTO workers(id,name,worker_type,status,capabilities_json,config_json) VALUES('w','I','infrastructure','available',?, '{}')",(json.dumps(MUTATING),));db.execute("INSERT INTO environment_targets(id,name,target_type,address,credential_ref,enabled,status,capabilities_json,config_json) VALUES('e','E','infrastructure','https://unit.invalid','ref',1,'available',?,?)",(json.dumps(MUTATING),json.dumps(cfg)));executor=InfrastructureToolsExecutor(Secrets());executor.task_poll_attempts=1
 def request(*args,**kwargs):
  if 'cluster/resources' in args[3]: return [{"vmid":120,"status":"stopped","node":"node-a"}]
  if (args[4] if len(args)>4 else kwargs.get('method'))=='POST': return 'UPID:unit'
  return {"status":"running"}
 executor._request=request;monkeypatch.setattr('virtizai_core.infra_tools.time.sleep',lambda _:None);b=WorkerExecutionBoundary(db);b.register(executor);assert run(b,'start_vm',{'vm_id':'120'}).error_summary=='postcondition_timeout';db.close()
