import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from virtizai_core.db import Database
from virtizai_core.discord import DiscordAdapter
from virtizai_core.interfaces import InterfaceRequest, InterfaceService
from virtizai_core.orchestration import AgentWorkRequest
from virtizai_core.services import SecretaryResponse, SessionService
from virtizai_core.delegation_policy import DelegationPolicyEngine

class FakeDelegation:
 def __init__(self,db): self.db=db;self.requests=[]
 async def delegate_agent(self,request):
  self.requests.append(request);self.db.execute("INSERT INTO messages(id,session_id,role,content,metadata_json) VALUES('m',?,'assistant','done',?)",(request.session_id,json.dumps({"job_id":"j"})))
  return {"id":"j","status":"succeeded","result_json":json.dumps({"output":{"final_summary":"done"}}),"result_summary":"done","error_summary":None}
def setup(tmp):
 db=Database(tmp/'s.db');db.open();db.execute("INSERT INTO users(id,display_name) VALUES('u','U')");db.execute("INSERT INTO providers(id,name,adapter_type) VALUES('p','P','mock')");db.execute("INSERT INTO models(id,provider_id,name) VALUES('m','p','M')");db.execute("UPDATE roles SET enabled=1 WHERE id='role-coding'");db.execute("INSERT INTO workers(id,name,worker_type) VALUES('w','W','dev_tools')");db.execute("INSERT INTO environment_targets(id,name,target_type) VALUES('e','E','workspace')");db.execute("INSERT INTO routes(id,name,role_id,priority,policy_json) VALUES('r','Coding','role-coding',1,?)",(json.dumps({"delegated_execution":{"worker_id":"w","environment_id":"e"}}),));db.execute("INSERT INTO route_targets(route_id,provider_id,model_id,ordinal) VALUES('r','p','m',0)")
 fake=FakeDelegation(db);svc=InterfaceService(db,SimpleNamespace(sessions=SessionService(db)),fake);return db,svc,fake
@pytest.mark.asyncio
async def test_generic_interface_delegation_resolves_canonical_persisted_plan(tmp_path):
 db,svc,fake=setup(tmp_path);req=InterfaceRequest('cli','subject','ignored');sid,response=await svc.delegate_for_session(req,'role-coding','inspect')
 assert response.content=='done' and len(fake.requests)==1
 got=fake.requests[0];assert (got.role_id,got.provider_id,got.model_id,got.worker_id,got.environment_id)==('role-coding','p','m','w','e')
 assert db.fetch_one("SELECT affinity_provider_id FROM sessions WHERE id=?",(sid,))[0] is None
 db.close()
@pytest.mark.asyncio
async def test_discord_explicit_trigger_calls_generic_interface_role(tmp_path):
 class Interfaces:
  async def handle(self,request):
   self.args=request;return 's',SecretaryResponse('r','s','m','done',None,'p','m',job_created=True,task_class='delegated')
 interfaces=Interfaces();adapter=DiscordAdapter(interfaces,SimpleNamespace())
 reply=await adapter.handle_message('user','/coding inspect README',session_key='k')
 assert interfaces.args.content=='/coding inspect README' and reply.content=='done'

def test_hybrid_deterministic_rules_are_conservative(tmp_path):
 db,_,_=setup(tmp_path);policy=DelegationPolicyEngine(db)
 assert policy.deterministic('/coding inspect README.md').role_id=='role-coding'
 assert policy.deterministic('/project plan an implementation and validation project').role_id=='role-project-lead'
 assert policy.deterministic('Plan a multi-step feature implementation and validation').role_id=='role-project-lead'
 assert policy.deterministic('Add a small improvements page to the WebUI that shows current environment health, add tests, and report what changed.').role_id=='role-project-lead'
 assert policy.deterministic('Inspect the WebUI health display, make one small clarity improvement, add a regression test, run the focused tests, and summarize the result.').role_id=='role-project-lead'
 assert policy.deterministic('Fix the source file parser').role_id=='role-coding'
 assert policy.deterministic('Restart the approved test VM').role_id=='role-infrastructure'
 assert policy.deterministic('Explain this architecture') is None
 assert policy.deterministic('What is Python?') is None
 db.close()

@pytest.mark.asyncio
async def test_model_classifier_confidence_and_malformed_output(tmp_path):
 db,_,_=setup(tmp_path)
 class Provider:
  def __init__(self,args): self.args=args
  async def chat(self,*args,**kwargs): return SimpleNamespace(tool_calls=(self.args,))
 async def candidates(): return [SimpleNamespace(provider_id='p',model_name='M')]
 good={"function":{"name":"delegation_decision","arguments":{"decision":"delegate","role_id":"role-coding","confidence":0.9,"reason_code":"coding_request"}}}
 assert (await DelegationPolicyEngine(db,Provider(good),candidates).decide('please help')).decision=='delegate'
 low={"function":{"name":"delegation_decision","arguments":{"decision":"delegate","role_id":"role-coding","confidence":0.5,"reason_code":"coding_request"}}}
 assert (await DelegationPolicyEngine(db,Provider(low),candidates).decide('please help')).decision=='direct'
 bad={"function":{"name":"delegation_decision","arguments":"not-json"}}
 assert (await DelegationPolicyEngine(db,Provider(bad),candidates).decide('please help')).decision=='direct'
 db.close()

@pytest.mark.asyncio
async def test_single_fallback_after_read_only_tool_execution(tmp_path):
 db,svc,_=setup(tmp_path)
 # Add a second explicit target and make capability routing legacy-compatible.
 db.execute("INSERT INTO providers(id,name,adapter_type,health_status) VALUES('p2','P2','mock','healthy')")
 db.execute("INSERT INTO models(id,provider_id,name,status) VALUES('m2','p2','M2','available')")
 db.execute("INSERT INTO route_targets(route_id,provider_id,model_id,ordinal) VALUES('r','p2','m2',1)")
 class Fallback:
  def __init__(self): self.requests=[]
  async def delegate_agent(self, request):
   self.requests.append(request)
   if len(self.requests)==1: return {'id':'one','status':'failed','result_json':json.dumps({'trace':[{'operation':'inspect_file','status':'succeeded'}], 'error_summary':'Coding Agent returned malformed tool call'}), 'error_summary':'Coding Agent returned malformed tool call'}
   return {'id':'two','status':'succeeded','result_json':json.dumps({'output':{'final_summary':'ok'},'trace':[]}), 'result_summary':'ok','error_summary':None}
 svc.delegation=Fallback();req=InterfaceRequest('cli','fallback','inspect');_,response=await svc.delegate_for_session(req,'role-coding','inspect')
 assert response.content=='ok' and len(svc.delegation.requests)==2 and svc.delegation.requests[1].context['routing_decision']['fallback_used'] is True and svc.delegation.requests[1].context['prior_read_evidence'][0]['operation']=='inspect_file'
 db.close()

def test_natural_infrastructure_reads_route_without_catching_education(tmp_path):
    db, _, _ = setup(tmp_path)
    policy = DelegationPolicyEngine(db)

    operational_reads = (
        "how many vms do i have?",
        "which VMs are running?",
        "show my VMs",
        "are any hosts down?",
        "are there any running services?",
        "what services are running?",
        "list the VMs",
        "which container is stopped?",
        "inspect my host",
        "how many of my containers are running?",
    )
    for text in operational_reads:
        decision = policy.deterministic(text)
        assert decision is not None, text
        assert decision.role_id == "role-infrastructure", text
        assert decision.reason_code == "bounded_infrastructure_read", text

    educational_or_advisory = (
        "what is a VM?",
        "how does a virtual machine work?",
        "explain containers",
        "what is Proxmox?",
        "how many VMs can Proxmox support?",
        "which VM technology is best?",
        "what is a container?",
    )
    for text in educational_or_advisory:
        decision = policy.deterministic(text)
        assert decision is None or decision.role_id != "role-infrastructure", text

    mutation = policy.deterministic("Restart the approved test VM")
    assert mutation is not None
    assert mutation.role_id == "role-infrastructure"
    assert mutation.reason_code == "bounded_infrastructure_mutation"

    coding = policy.deterministic("/coding inspect README.md")
    assert coding is not None
    assert coding.role_id == "role-coding"

    project = policy.deterministic("Plan a multi-step feature implementation")
    assert project is not None
    assert project.role_id == "role-project-lead"

    db.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('follow_up', ('list them for me', 'which ones are running?'))
async def test_successful_same_session_infrastructure_read_resolves_list_follow_up(tmp_path, follow_up):
 db, svc, fake = setup(tmp_path)
 db.execute("UPDATE roles SET enabled=1 WHERE id='role-infrastructure'")
 db.execute("INSERT INTO routes(id,name,role_id,priority,policy_json) VALUES('ri','Infrastructure','role-infrastructure',1,?)", (json.dumps({"delegated_execution":{"worker_id":"w","environment_id":"e"}}),))
 db.execute("INSERT INTO route_targets(route_id,provider_id,model_id,ordinal) VALUES('ri','p','m',0)")
 request = InterfaceRequest('cli', 'same-session', 'how many vms do i have?')
 session_id = svc.resolve_session(request)
 user_id = db.fetch_one("SELECT user_id FROM interface_sessions WHERE session_id=?", (session_id,))[0]
 result = json.dumps({"trace":[{"operation":"list_vms","status":"succeeded"}], "output":{"final_summary":"You have 2 VM/LXC resources in total."}})
 db.execute("INSERT INTO jobs(id,user_id,session_id,kind,status,payload_json,result_json) VALUES('live',?,?, 'delegated_agent','succeeded','{}',?)", (user_id, session_id, result))
 db.execute("INSERT INTO messages(id,session_id,role,content,metadata_json) VALUES('prior',?,'assistant','live inventory',?)", (session_id, json.dumps({"job_id":"live","role_id":"role-infrastructure","status":"succeeded","operation":"list_vms"})))
 _, response = await svc.handle(InterfaceRequest('cli', 'same-session', follow_up))
 assert fake.requests[-1].role_id == 'role-infrastructure'
 assert fake.requests[-1].objective == 'list my VM/container infrastructure resources'
 assert response.job_created is True
 db.close()

@pytest.mark.asyncio
async def test_ambiguous_inventory_follow_up_never_infers_infrastructure_without_live_session_evidence(tmp_path):
 db, svc, fake = setup(tmp_path)
 session_id, response = await svc.handle(InterfaceRequest('cli', 'unrelated-session', 'list them'))
 assert response.task_class == 'secretary'
 assert not fake.requests
 assert 'successful live infrastructure inventory' in response.content
 assert not svc._has_successful_infrastructure_read(session_id)
 assert (await svc.delegation_policy.decide('what are they?')).role_id is None
 db.close()
