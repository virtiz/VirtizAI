from __future__ import annotations
import json
from pathlib import Path
import pytest
from virtizai_core.adapters import InferenceResponse
from virtizai_core.db import Database
from virtizai_core.dev_tools import DevelopmentToolsExecutor
from virtizai_core.jobs import JobManager
from virtizai_core.orchestration import AgentWorkRequest, DelegationService
from virtizai_core.workers import ExecutionResult


def test_infrastructure_tool_feedback_preserves_bounded_normalized_result():
    result = ExecutionResult("succeeded", {"id": "120", "state": "running", "host": "node-a", "nested": {"secret": "not-a-secret"}})
    feedback = json.loads(DelegationService._tool_feedback("inspect_vm", result))
    assert feedback["result"]["id"] == "120"
    assert feedback["result"]["state"] == "running"
    assert feedback["result"]["host"] == "node-a"


def test_infrastructure_mutation_tool_visibility_requires_persisted_policy(tmp_path):
    db = Database(tmp_path / "tools.db"); db.open()
    db.execute("INSERT INTO environment_targets(id,name,target_type,enabled,status,capabilities_json,config_json) VALUES('e','E','infrastructure',1,'available',?,?)", (json.dumps(["read_infrastructure", "inspect_vm", "start_vm", "restart_vm"]), json.dumps({"allowed_resource_ids":["120"], "operation_resource_ids":{"start_vm":["121"]}, "allowed_risk_classes":["MUTATING_REVERSIBLE"]})))
    tools = DelegationService(db, JobManager(db), WorkerExecutionBoundary(db))._infrastructure_tools("e")
    by_name = {item["function"]["name"]: item["function"] for item in tools}
    assert by_name["inspect_vm"]["parameters"]["properties"]["vm_id"]["enum"] == ["120"]
    assert by_name["start_vm"]["parameters"]["properties"]["vm_id"]["enum"] == ["121"]
    assert "start_vm" in by_name and "restart_vm" not in by_name
    assert "command" not in by_name and "delete_vm" not in by_name
    db.close()


def test_coding_tests_are_not_offered_before_inspection():
    assert {item["function"]["name"] for item in DelegationService._agent_tools(False)} == {"inspect_file", "replace_text"}
    assert "run_tests" in {item["function"]["name"] for item in DelegationService._agent_tools(True)}


def test_coding_tool_schema_describes_only_persisted_workspace_roots():
    tools = DelegationService._agent_tools(False, ["virtizai_core", "webui"])
    inspect = next(item["function"] for item in tools if item["function"]["name"] == "inspect_file")
    assert "virtizai_core, webui" in inspect["description"]
    assert "virtizai_core, webui" in inspect["parameters"]["properties"]["path"]["description"]
from virtizai_core.registries import EnvironmentRegistry, WorkerRegistry
from virtizai_core.workers import WorkerExecutionBoundary

def call(name,args): return {"id":"c","type":"function","function":{"name":name,"arguments":json.dumps(args)}}
class Provider:
 def __init__(self,items): self.items=list(items);self.calls=[]
 async def chat(self,*args,tools=None,tool_choice=None,**kwargs):
  self.calls.append((tools,tool_choice,args[2])); item=self.items.pop(0)
  if isinstance(item,Exception): raise item
  return InferenceResponse(item[1],"agent",None,None,None,None,1,True,tool_calls=tuple(item[0]))
class Boundary(WorkerExecutionBoundary):
 def __init__(self,db): super().__init__(db);self.calls=[]
 async def execute(self,req): self.calls.append(req);return await super().execute(req)
def setup(tmp,items,text="before\n"):
 db=Database(tmp/"state.db");db.open();db.execute("INSERT INTO users(id,display_name) VALUES('u','U')");db.execute("INSERT INTO providers(id,name,adapter_type) VALUES('sp','S','mock'),('ap','A','mock')");db.execute("INSERT INTO models(id,provider_id,name) VALUES('sm','sp','s'),('am','ap','agent')");db.execute("INSERT INTO sessions(id,user_id,affinity_provider_id,affinity_model_id) VALUES('s','u','sp','sm')");db.execute("UPDATE roles SET enabled=1 WHERE id='role-coding'")
 ws=tmp/"ws";ws.mkdir();target=ws/"src/file.txt";target.parent.mkdir();target.write_text(text)
 w=WorkerRegistry(db).create("w","dev_tools");e=EnvironmentRegistry(db).create("e","workspace");db.execute("UPDATE environment_targets SET config_json=? WHERE id=?",(json.dumps({"workspace_path":str(ws),"allowed_roots":["src"]}),e))
 b=Boundary(db);b.register(DevelopmentToolsExecutor());p=Provider(items);svc=DelegationService(db,JobManager(db),b,p);req=AgentWorkRequest('s','role-coding','ap','am',w,e,'replace text',timeout_seconds=10);return db,target,b,p,svc,req
def inspect(): return call("inspect_file",{"path":"src/file.txt","max_lines":20})
def replace(old="before",new="after",path="src/file.txt"): return call("replace_text",{"path":path,"old_text":old,"new_text":new})
def affinity(db): return dict(db.fetch_one("SELECT affinity_provider_id,affinity_model_id FROM sessions WHERE id='s'"))
@pytest.mark.asyncio
async def test_structured_tool_and_valid_replacement_use_internal_apply_patch(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[([inspect()],""),([replace()],""),((),"done")]);job=await svc.delegate_agent(req);data=json.loads(job["result_json"])
 names={x["function"]["name"] for x in p.calls[0][0]};tool=next(x["function"] for x in p.calls[0][0] if x["function"]["name"]=="replace_text")
 assert job["status"]=="succeeded" and target.read_text()=="after\n" and names=={"inspect_file","replace_text"} and "apply_patch" not in names
 assert tool["parameters"]["required"]==["path","old_text","new_text"] and [x.operation for x in b.calls]==["inspect_file","apply_patch"] and data["trace"][1]["operation"]=="replace_text"
 assert affinity(db)=={"affinity_provider_id":"sp","affinity_model_id":"sm"};db.close()
@pytest.mark.asyncio
@pytest.mark.parametrize(("intent","code"),[(replace("missing"),"mutation_old_text_missing"),(replace("x"),"mutation_old_text_ambiguous"),(replace(path="/tmp/x"),"mutation_path_invalid"),(replace(path="../x"),"mutation_path_invalid"),(replace("x"*4001),"mutation_text_too_large")])
async def test_mutation_rejections_are_safe_and_bounded(tmp_path,intent,code):
 text="x x\n" if code=="mutation_old_text_ambiguous" else "before\n";db,target,b,p,svc,req=setup(tmp_path,[([inspect()],""),([intent],"")],text);original=target.read_text();job=await svc.delegate_agent(req);d=json.loads(job["result_json"])["rejection_diagnostic"]
 assert job["status"]=="failed" and d["rejection_code"]==code and d["operation"]=="replace_text" and target.read_text()==original and [x.operation for x in b.calls]==["inspect_file"]
 assert intent["function"]["arguments"] not in job["result_json"] and affinity(db)=={"affinity_provider_id":"sp","affinity_model_id":"sm"};db.close()
@pytest.mark.asyncio
async def test_path_not_inspected_and_one_mutation_limit(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[([replace()],"")]);job=await svc.delegate_agent(req);assert json.loads(job["result_json"])["rejection_diagnostic"]["rejection_code"]=="mutation_path_not_inspected" and not b.calls;db.close()
 db,target,b,p,svc,req=setup(tmp_path/"two",[([inspect()],""),([replace()],""),([replace("after","again")],"")]);job=await svc.delegate_agent(req);assert job["status"]=="failed" and job["error_summary"]=="Coding Agent exceeded mutation limit" and target.read_text()=="after\n";db.close()

@pytest.mark.asyncio
async def test_missing_inspection_is_bounded_feedback_then_model_can_recover(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[([call("inspect_file",{"path":"src/missing.txt"})],""),([inspect()],""),((),"inspected the available file")])
 job=await svc.delegate_agent(req); data=json.loads(job["result_json"])
 assert job["status"]=="succeeded" and [item.operation for item in b.calls]==["inspect_file","inspect_file"]
 assert [item["status"] for item in data["trace"]]==["failed","succeeded"]
 assert any("File not found" in str(message.get("content", "")) for message in p.calls[1][2] if isinstance(message,dict))
 assert affinity(db)=={"affinity_provider_id":"sp","affinity_model_id":"sm"};db.close()
@pytest.mark.asyncio
async def test_provider_and_worker_failures_preserve_affinity(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[([inspect()],""),RuntimeError("x")]);job=await svc.delegate_agent(req);assert job["status"]=="failed" and affinity(db)=={"affinity_provider_id":"sp","affinity_model_id":"sm"};db.close()
 db,target,b,p,svc,req=setup(tmp_path/"worker",[([inspect()],""),([replace("missing")],"")]);job=await svc.delegate_agent(req);assert job["status"]=="failed" and target.read_text()=="before\n";db.close()
