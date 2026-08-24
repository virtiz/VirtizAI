import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from virtizai_core.db import Database
from virtizai_core.discord import DiscordAdapter
from virtizai_core.interfaces import InterfaceRequest, InterfaceService
from virtizai_core.orchestration import AgentWorkRequest
from virtizai_core.services import SecretaryResponse, SessionService

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
  async def delegate_for_session(self,request,role,objective):
   self.args=(request,role,objective);return 's',SecretaryResponse('r','s','m','done',None,'p','m',job_created=True,task_class='delegated')
  async def handle(self,request): raise AssertionError('legacy path')
 interfaces=Interfaces();adapter=DiscordAdapter(interfaces,SimpleNamespace())
 reply=await adapter.handle_message('user','/coding inspect README',session_key='k')
 assert interfaces.args[1:]==('role-coding','inspect README') and reply.content=='done'
