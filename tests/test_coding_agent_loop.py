from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace
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
    assert {item["function"]["name"] for item in DelegationService._agent_tools(False)} == {"list_files", "inspect_file", "replace_text"}
    assert "run_tests" in {item["function"]["name"] for item in DelegationService._agent_tools(True)}


def test_coding_tool_schema_describes_only_persisted_workspace_roots():
    tools = DelegationService._agent_tools(False, ["virtizai_core", "webui"])
    inspect = next(item["function"] for item in tools if item["function"]["name"] == "inspect_file")
    assert "virtizai_core, webui" in inspect["description"]
    assert "virtizai_core, webui" in inspect["parameters"]["properties"]["path"]["description"]


def test_coding_file_listing_can_be_withheld_when_bounded_index_is_available():
    names = {item["function"]["name"] for item in DelegationService._agent_tools(False, ["src"], include_file_listing=False)}
    assert names == {"inspect_file", "replace_text"}


def test_coding_allows_one_bounded_focused_test_path_only():
    assert DelegationService._is_allowed_test_target("tests/test_webui.py")
    assert DelegationService._is_allowed_test_target("tests/test_webui.py::test_page")
    assert not DelegationService._is_allowed_test_target("tests/test_webui.py -k injected")
    assert not DelegationService._is_allowed_test_target("../tests/test_webui.py")
from virtizai_core.registries import EnvironmentRegistry, WorkerRegistry
from virtizai_core.workers import WorkerExecutionBoundary

def call(name,args): return {"id":"c","type":"function","function":{"name":name,"arguments":json.dumps(args)}}
class Provider:
 def __init__(self,items): self.items=list(items);self.calls=[];self.max_tokens=[]
 async def chat(self,*args,tools=None,tool_choice=None,**kwargs):
  self.calls.append((tools,tool_choice,args[2]));self.max_tokens.append(kwargs.get("max_tokens")); item=self.items.pop(0)
  if isinstance(item,Exception): raise item
  return InferenceResponse(item[1],"agent",None,None,None,None,1,True,tool_calls=tuple(item[0]))
class Boundary(WorkerExecutionBoundary):
 def __init__(self,db): super().__init__(db);self.calls=[]
 async def execute(self,req): self.calls.append(req);return await super().execute(req)
def setup(tmp,items,text="before\n"):
 db=Database(tmp/"state.db");db.open();db.execute("INSERT INTO users(id,display_name) VALUES('u','U')");db.execute("INSERT INTO providers(id,name,adapter_type) VALUES('sp','S','mock'),('ap','A','mock')");db.execute("INSERT INTO models(id,provider_id,name) VALUES('sm','sp','s'),('am','ap','agent')");db.execute("INSERT INTO sessions(id,user_id,affinity_provider_id,affinity_model_id) VALUES('s','u','sp','sm')");db.execute("UPDATE roles SET enabled=1 WHERE id='role-coding'")
 ws=tmp/"ws";ws.mkdir();target=ws/"src/file.txt";target.parent.mkdir();target.write_text(text)
 tests_dir=ws/"tests";tests_dir.mkdir();(tests_dir/"test_smoke.py").write_text("def test_smoke():\n    assert True\n")
 w=WorkerRegistry(db).create("w","dev_tools");e=EnvironmentRegistry(db).create("e","workspace");db.execute("UPDATE environment_targets SET config_json=? WHERE id=?",(json.dumps({"workspace_path":str(ws),"allowed_roots":["src","tests"]}),e))
 b=Boundary(db);b.register(DevelopmentToolsExecutor());p=Provider(items);svc=DelegationService(db,JobManager(db),b,p);req=AgentWorkRequest('s','role-coding','ap','am',w,e,'replace text',timeout_seconds=10);return db,target,b,p,svc,req
def inspect(): return call("inspect_file",{"path":"src/file.txt","max_lines":20})
def replace(old="before",new="after",path="src/file.txt"): return call("replace_text",{"path":path,"old_text":old,"new_text":new})
def affinity(db): return dict(db.fetch_one("SELECT affinity_provider_id,affinity_model_id FROM sessions WHERE id='s'"))
@pytest.mark.asyncio
async def test_structured_tool_and_valid_replacement_use_native_replace_text(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[([inspect()],""),([replace()],""),((),"done")]);job=await svc.delegate_agent(req);data=json.loads(job["result_json"])
 names={x["function"]["name"] for x in p.calls[0][0]};tool=next(x["function"] for x in p.calls[0][0] if x["function"]["name"]=="replace_text")
 assert job["status"]=="succeeded" and target.read_text()=="after\n" and names=={"inspect_file","replace_text"} and "apply_patch" not in names
 assert tool["parameters"]["required"]==["path","old_text","new_text"] and [x.operation for x in b.calls]==["inspect_file","replace_text"] and data["trace"][1]["operation"]=="replace_text"
 assert affinity(db)=={"affinity_provider_id":"sp","affinity_model_id":"sm"};db.close()
@pytest.mark.asyncio
@pytest.mark.parametrize(
 ("intent","expected_error","worker_called","recoverable"),
 [
  (replace("missing"),"old_text_missing",True,True),
  (replace("x"),"old_text_ambiguous",True,True),
  (replace(path="/tmp/x"),"Coding Agent selected an invalid mutation intent",False,False),
  (replace(path="../x"),"Coding Agent selected an invalid mutation intent",False,False),
  (replace("x"*4001),"Coding Agent selected an invalid mutation intent",False,False),
 ],
)
async def test_mutation_rejections_are_safe_and_bounded(tmp_path,intent,expected_error,worker_called,recoverable):
 text="x x\n" if expected_error=="old_text_ambiguous" else "before\n"
 responses=[([inspect()],""),([intent],"")]
 if recoverable:
  responses.append(((),"unable to recover"))
 db,target,b,p,svc,req=setup(tmp_path,responses,text)
 original=target.read_text()
 job=await svc.delegate_agent(req)
 data=json.loads(job["result_json"])
 assert job["status"]=="failed"
 assert expected_error in job["error_summary"]
 assert target.read_text()==original
 assert [x.operation for x in b.calls]==(["inspect_file","replace_text"] if worker_called else ["inspect_file"])
 assert intent["function"]["arguments"] not in job["result_json"]
 assert affinity(db)=={"affinity_provider_id":"sp","affinity_model_id":"sm"}
 if recoverable:
  assert data["output"]["termination"]=="mutation_recovery_incomplete"
  assert data["side_effect_state"]=="READ_ONLY"
 db.close()
@pytest.mark.asyncio
async def test_path_not_inspected_and_two_mutation_limit(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[([replace()],"")]);job=await svc.delegate_agent(req);assert json.loads(job["result_json"])["rejection_diagnostic"]["rejection_code"]=="mutation_path_not_inspected" and not b.calls;db.close()

 db,target,b,p,svc,req=setup(
  tmp_path/"two",
  [
   ([inspect()],""),
   ([replace()],""),
   ([inspect()],""),
   ([replace("after","again")],""),
   ([replace("again","third")],""),
  ],
 )
 job=await svc.delegate_agent(req)
 assert job["status"]=="failed"
 assert "replace_text" in job["error_summary"]
 assert "not offered" in job["error_summary"]
 assert target.read_text()=="before\n"
 assert [item.operation for item in b.calls]==["inspect_file","replace_text","inspect_file","replace_text"]
 assert json.loads(job["result_json"])["rollback"]["status"]=="succeeded"
 db.close()

@pytest.mark.asyncio
async def test_missing_inspection_is_bounded_feedback_then_model_can_recover(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[([call("inspect_file",{"path":"src/missing.txt"})],""),([inspect()],""),((),"inspected the available file")])
 job=await svc.delegate_agent(req); data=json.loads(job["result_json"])
 assert job["status"]=="succeeded" and [item.operation for item in b.calls]==["inspect_file","inspect_file"]
 assert [item["status"] for item in data["trace"]]==["failed","succeeded"]
 assert any("File not found" in str(message.get("content", "")) for message in p.calls[1][2] if isinstance(message,dict))
 assert affinity(db)=={"affinity_provider_id":"sp","affinity_model_id":"sm"};db.close()

@pytest.mark.asyncio
async def test_coding_agent_uses_tool_sized_completion_budget(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[([inspect()],"") ,((),"done")])
 job=await svc.delegate_agent(req)
 assert job["status"]=="succeeded"
 assert p.max_tokens==[
  DelegationService.CODING_AGENT_MAX_TOKENS,
  DelegationService.CODING_AGENT_MAX_TOKENS,
 ]
 assert DelegationService.CODING_AGENT_MAX_TOKENS==2048
 db.close()


@pytest.mark.asyncio
async def test_multiple_read_only_inspections_are_serialized_to_one_action(tmp_path):
 db,target,b,p,svc,req=setup(
  tmp_path,
  [
   ([inspect(), call("inspect_file", {"path": "tests/test_smoke.py"})], ""),
   ((), "done"),
  ],
 )
 job=await svc.delegate_agent(req)

 assert job["status"]=="succeeded"
 assert [item.operation for item in b.calls]==["inspect_file"]
 assert json.loads(job["result_json"])["output"]["trace"][0]["operation"]=="inspect_file"
 assert target.read_text()=="before\n"

 db.close()


@pytest.mark.asyncio
async def test_multiple_calls_still_reject_when_any_call_is_not_read_only_inspection(tmp_path):
 db,target,b,p,svc,req=setup(
  tmp_path,
  [
   ([inspect(), replace()], ""),
  ],
 )
 job=await svc.delegate_agent(req)

 assert job["status"]=="failed"
 assert job["error_summary"]=="Coding Agent returned multiple tool calls"
 assert b.calls==[]
 assert target.read_text()=="before\n"

 db.close()


@pytest.mark.asyncio
async def test_unstructured_truncated_tool_markup_cannot_be_final_success(tmp_path):
 raw='<tool_call>replace_text<arg_key>path</arg_key><arg_value>src/file.txt</arg_value>'
 db,target,b,p,svc,req=setup(tmp_path,[([inspect()],"") ,((),raw)])
 job=await svc.delegate_agent(req)

 assert job["status"]=="failed"
 assert job["error_summary"]=="Coding Agent returned an incomplete or unstructured tool call"
 assert [item.operation for item in b.calls]==["inspect_file"]
 assert target.read_text()=="before\n"

 db.close()


@pytest.mark.asyncio
async def test_unoffered_list_files_is_rejected_before_worker_execution(tmp_path):
    db,target,b,p,svc,req=setup(
        tmp_path,
        [([call("list_files",{})],"")],
    )

    job=await svc.delegate_agent(req)

    offered_names={item["function"]["name"] for item in p.calls[0][0]}

    assert "list_files" not in offered_names
    assert job["status"]=="failed"
    assert "list_files" in job["error_summary"]
    assert "not offered" in job["error_summary"]
    assert b.calls==[]
    assert target.read_text()=="before\n"

    db.close()

@pytest.mark.asyncio
async def test_provider_and_worker_failures_preserve_affinity(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[([inspect()],""),RuntimeError("x")]);job=await svc.delegate_agent(req);assert job["status"]=="failed" and affinity(db)=={"affinity_provider_id":"sp","affinity_model_id":"sm"};db.close()
 db,target,b,p,svc,req=setup(tmp_path/"worker",[([inspect()],""),([replace("missing")],"")]);job=await svc.delegate_agent(req);assert job["status"]=="failed" and target.read_text()=="before\n";db.close()


def test_bounded_file_discovery_is_fair_deterministic_and_cache_free(tmp_path):
    db,target,b,p,svc,req=setup(tmp_path,[])
    workspace=target.parents[1]

    early=workspace/"early"
    early.mkdir()

    for index in range(100):
        (early/f"file_{index:03d}.txt").write_text(str(index))

    cache=early/"__pycache__"
    cache.mkdir()
    (cache/"junk.pyc").write_bytes(b"cache")
    (early/"standalone.pyc").write_bytes(b"cache")
    (early/"standalone.pyo").write_bytes(b"cache")

    webui=workspace/"webui"
    webui.mkdir()
    (webui/"health.js").write_text("export const health = true;\\n")

    env_id=db.fetch_one(
        "SELECT id FROM environment_targets ORDER BY created_at DESC LIMIT 1"
    )["id"]

    db.execute(
        "UPDATE environment_targets SET config_json=? WHERE id=?",
        (
            json.dumps({
                "workspace_path":str(workspace),
                "allowed_roots":["early","tests","webui"],
            }),
            env_id,
        ),
    )

    first=svc._coding_file_index(env_id)
    second=svc._coding_file_index(env_id)

    assert first==second
    assert len(first)<=80
    assert "webui/health.js" in first
    assert not any("__pycache__" in item for item in first)
    assert not any(item.endswith(".pyc") for item in first)
    assert not any(item.endswith(".pyo") for item in first)

    environment=dict(
        db.fetch_one("SELECT * FROM environment_targets WHERE id=?", (env_id,))
    )
    runtime=DevelopmentToolsExecutor()._list_files(
        SimpleNamespace(payload={}),
        environment,
    )

    assert runtime.status=="succeeded"
    runtime_files=runtime.output["files"]
    assert runtime_files==first
    assert len(runtime_files)<=80
    assert "webui/health.js" in runtime_files
    assert not any("__pycache__" in item for item in runtime_files)
    assert not any(item.endswith(".pyc") for item in runtime_files)
    assert not any(item.endswith(".pyo") for item in runtime_files)
    assert runtime.output["truncated"] is True

    db.close()


# Budget exhaustion regression tests
@pytest.mark.asyncio
async def test_budget_exhaustion_at_10_inference_limit_with_empty_content(tmp_path):
 db,target,b,p,svc,req=setup(
  tmp_path,
  [([call("list_files", {})],"") for _ in range(10)],
 )
 svc._coding_file_index=lambda environment_id: []
 job=await svc.delegate_agent(req)
 data=json.loads(job["result_json"])

 assert job["status"]=="failed"
 assert len(data["trace"])==10
 assert [item["operation"] for item in data["trace"]]==["list_files"]*10
 assert data["output"]["termination"]=="coding_budget_exhausted"
 assert data["output"]["final_summary"]=="Coding budget exhausted without completing objective"
 assert job["error_summary"]=="Coding Agent: Coding budget exhausted without completing objective"
 assert target.read_text()=="before\n"

 db.close()


@pytest.mark.asyncio
async def test_budget_exhaustion_at_10_inference_limit_with_content_summary(tmp_path):
 responses=[([call("list_files", {})],"") for _ in range(9)]
 responses.append(([call("list_files", {})],"summary at 10"))

 db,target,b,p,svc,req=setup(tmp_path,responses)
 svc._coding_file_index=lambda environment_id: []
 job=await svc.delegate_agent(req)
 data=json.loads(job["result_json"])

 assert job["status"]=="failed"
 assert len(data["trace"])==10
 assert [item["operation"] for item in data["trace"]]==["list_files"]*10
 assert data["output"]["termination"]=="coding_budget_exhausted"
 assert data["output"]["final_summary"]=="summary at 10"
 assert job["error_summary"]=="Coding Agent: summary at 10"
 assert target.read_text()=="before\n"

 db.close()


@pytest.mark.asyncio
async def test_bounded_inspect_mutate_test_and_final_response_persists_meaningful_summary(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace()],""),
  ([call("run_tests",{"target":"tests/test_smoke.py"})],""),
  ((),"Updated src/file.txt and focused test passed"),
 ])
 job=await svc.delegate_agent(req);data=json.loads(job["result_json"])
 assert job["status"]=="succeeded"
 assert [item.operation for item in b.calls]==["inspect_file","replace_text","run_tests"]
 assert target.read_text()=="after\n"
 assert data["output"]["termination"]=="final_response"
 assert data["output"]["final_summary"]=="Updated src/file.txt and focused test passed"
 assert job["result_summary"]=="Updated src/file.txt and focused test passed"
 assert data["side_effect_state"]=="MUTATED"
 db.close()

@pytest.mark.asyncio
async def test_two_sequential_native_edits_same_file_succeed_after_reinspection(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace()],""),
  ([inspect()],""),
  ([replace("after","again")],""),
  ([call("run_tests",{"target":"tests/test_smoke.py"})],""),
  ((),"done"),
 ])
 job=await svc.delegate_agent(req)
 assert job["status"]=="succeeded"
 assert target.read_text()=="again\n"
 assert [item.operation for item in b.calls]==["inspect_file","replace_text","inspect_file","replace_text","run_tests"]
 data=json.loads(job["result_json"])
 assert data["rollback"] is None
 assert data["trace"][1]["current_revision"]==data["trace"][0]["inspection_revision"]
 assert data["trace"][3]["current_revision"]==data["trace"][2]["inspection_revision"]
 db.close()


@pytest.mark.asyncio
async def test_native_edits_file_a_then_file_b_succeed(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace()],""),
  ([call("inspect_file",{"path":"src/second.txt","max_lines":20})],""),
  ([replace("second-before","second-after","src/second.txt")],""),
  ([call("run_tests",{"target":"tests/test_smoke.py"})],""),
  ((),"done"),
 ])
 second=target.parent/"second.txt"
 second.write_text("second-before\n")
 job=await svc.delegate_agent(req)
 assert job["status"]=="succeeded"
 assert target.read_text()=="after\n"
 assert second.read_text()=="second-after\n"
 assert [item.operation for item in b.calls]==["inspect_file","replace_text","inspect_file","replace_text","run_tests"]
 db.close()


@pytest.mark.asyncio
async def test_second_native_mutation_failure_rolls_back_first(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace()],""),
  ([inspect()],""),
  ([replace("missing","never")],""),
  ((),"unable to recover"),
 ])
 job=await svc.delegate_agent(req)
 data=json.loads(job["result_json"])
 assert job["status"]=="failed"
 assert job["error_summary"]=="old_text_missing"
 assert data["output"]["termination"]=="mutation_recovery_incomplete"
 assert target.read_text()=="before\n"
 assert data["rollback"]["status"]=="succeeded"
 assert data["rollback"]["restored"]==["src/file.txt"]
 db.close()


@pytest.mark.asyncio
async def test_failed_focused_test_rolls_back_native_mutation(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace()],""),
  ([call("run_tests",{"target":"tests/test_smoke.py"})],""),
 ])
 workspace=target.parents[1]
 (workspace/"tests/test_smoke.py").write_text("def test_smoke():\n    assert False\n")
 job=await svc.delegate_agent(req)
 data=json.loads(job["result_json"])
 assert job["status"]=="failed"
 assert target.read_text()=="before\n"
 assert data["rollback"]["status"]=="succeeded"
 db.close()


@pytest.mark.asyncio
async def test_failure_rollback_preserves_unrelated_preexisting_dirty_file(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace()],""),
  ([inspect()],""),
  ([replace("missing","never")],""),
 ])
 unrelated=target.parent/"unrelated.txt"
 unrelated.write_text("preexisting dirty content\n")
 job=await svc.delegate_agent(req)
 assert job["status"]=="failed"
 assert target.read_text()=="before\n"
 assert unrelated.read_text()=="preexisting dirty content\n"
 db.close()


@pytest.mark.asyncio
async def test_native_coding_job_diagnostics_are_sanitized(tmp_path):
 secret_old="before"
 secret_new="after-SENSITIVE-CONTENT"
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace(secret_old,secret_new)],""),
  ([call("run_tests",{"target":"tests/test_smoke.py"})],""),
  ((),"done"),
 ])
 job=await svc.delegate_agent(req)
 raw=job["result_json"]
 data=json.loads(raw)
 assert job["status"]=="succeeded"
 assert secret_new not in raw
 assert '"old_text"' not in raw
 assert '"new_text"' not in raw
 replace_trace=next(item for item in data["trace"] if item["operation"]=="replace_text")
 assert replace_trace["target_path"]=="src/file.txt"
 assert isinstance(replace_trace["current_revision"],str)
 assert isinstance(replace_trace["result_revision"],str)
 db.close()

def test_rollback_conflict_does_not_clobber_later_external_change(tmp_path):
 import hashlib

 db,target,b,p,svc,req=setup(tmp_path,[])
 original=(target.read_bytes(), target.stat().st_mode & 0o7777)

 target.write_text("job-produced\n")
 job_revision=hashlib.sha256(target.read_bytes()).hexdigest()

 target.write_text("later-external-change\n")
 diagnostic=svc._rollback_coding_mutations(
  req.environment_id,
  {"src/file.txt":original},
  {"src/file.txt"},
  {"src/file.txt":job_revision},
 )

 assert diagnostic["status"]=="failed"
 assert diagnostic["restored"]==[]
 assert diagnostic["conflicts"]==["src/file.txt"]
 assert diagnostic["failed"]==[]
 assert target.read_text()=="later-external-change\n"
 db.close()

@pytest.mark.asyncio
async def test_old_text_missing_recovers_then_mutates_tests_and_finishes(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace("not-present","after")],""),
  ([inspect()],""),
  ([replace()],""),
  ([call("run_tests",{"target":"tests/test_smoke.py"})],""),
  ((),"recovered and completed"),
 ])
 job=await svc.delegate_agent(req)
 data=json.loads(job["result_json"])
 assert job["status"]=="succeeded"
 assert target.read_text()=="after\n"
 assert [item.operation for item in b.calls]==[
  "inspect_file","replace_text","inspect_file","replace_text","run_tests"
 ]
 assert [item["status"] for item in data["trace"]]==[
  "succeeded","failed","succeeded","succeeded","succeeded"
 ]
 assert data["trace"][1]["error_code"]=="old_text_missing"
 assert data["side_effect_state"]=="MUTATED"
 assert data["rollback"] is None
 db.close()


@pytest.mark.asyncio
async def test_failed_replace_cannot_be_declared_success_without_correction(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace("missing","after")],""),
  ((),"done"),
 ])
 job=await svc.delegate_agent(req)
 data=json.loads(job["result_json"])
 assert job["status"]=="failed"
 assert job["error_summary"]=="old_text_missing"
 assert data["output"]["termination"]=="mutation_recovery_incomplete"
 assert data["side_effect_state"]=="READ_ONLY"
 assert target.read_text()=="before\n"
 db.close()


@pytest.mark.asyncio
async def test_stale_inspection_requires_fresh_inspect_before_successful_retry(tmp_path):
 import hashlib
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace()],""),
  ([inspect()],""),
  ([replace("external-before","after")],""),
  ([call("run_tests",{"target":"tests/test_smoke.py"})],""),
  ((),"done"),
 ])

 class StaleOnceBoundary(Boundary):
  def __init__(self,db,target):
   super().__init__(db)
   self.target=target
   self.injected=False
  async def execute(self,req):
   if req.operation=="replace_text" and not self.injected:
    self.target.write_text("external-before\n")
    self.injected=True
   return await super().execute(req)

 stale=StaleOnceBoundary(db,target)
 stale.register(DevelopmentToolsExecutor())
 svc.workers=stale
 job=await svc.delegate_agent(req)
 data=json.loads(job["result_json"])
 assert job["status"]=="succeeded"
 assert target.read_text()=="after\n"
 assert [item.operation for item in stale.calls]==[
  "inspect_file","replace_text","inspect_file","replace_text","run_tests"
 ]
 assert data["trace"][1]["status"]=="failed"
 assert data["trace"][1]["error_code"]=="stale_inspection"
 external_revision=hashlib.sha256(b"external-before\n").hexdigest()
 assert data["trace"][2]["inspection_revision"]==external_revision
 assert data["trace"][3]["current_revision"]==external_revision
 assert data["rollback"] is None
 db.close()


@pytest.mark.asyncio
async def test_replace_failure_feedback_contains_only_sanitized_error_code(tmp_path):
 db,target,b,p,svc,req=setup(tmp_path,[
  ([inspect()],""),
  ([replace("missing-SENSITIVE","after-SENSITIVE")],""),
  ((),"unable to recover"),
 ])
 job=await svc.delegate_agent(req)
 raw=job["result_json"]
 data=json.loads(raw)
 assert job["status"]=="failed"
 assert data["trace"][1]["error_code"]=="old_text_missing"
 assert "missing-SENSITIVE" not in raw
 assert "after-SENSITIVE" not in raw
 db.close()

def test_coding_mutation_target_rejects_allowed_root_that_escapes_workspace(tmp_path):
    from virtizai_core.orchestration import DelegationError

    db,target,b,p,svc,req=setup(tmp_path,[])
    workspace=target.parents[1]
    db.execute(
        "UPDATE environment_targets SET config_json=? WHERE id=?",
        (
            json.dumps({
                "workspace_path":str(workspace),
                "allowed_roots":[".."],
            }),
            req.environment_id,
        ),
    )

    with pytest.raises(DelegationError, match="allowed roots"):
        svc._coding_mutation_target(req.environment_id,"src/file.txt")

    assert target.read_text()=="before\n"
    db.close()
