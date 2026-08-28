import asyncio
import json
from pathlib import Path
import subprocess

import pytest

from virtizai_core.db import Database
from virtizai_core.project_lead import ProjectLeadService
from virtizai_core.workers import ExecutionRequest, ManagedPlanningWorkerExecutor, WorkerExecutionBoundary, WorkerExecutionError


def setup_planner(tmp_path):
    workspace=tmp_path/'repo'; workspace.mkdir(); (workspace/'README.md').write_text('hello\n')
    subprocess.run(['git','init'],cwd=workspace,check=True,stdout=subprocess.DEVNULL)
    subprocess.run(['git','config','user.email','test@example.invalid'],cwd=workspace,check=True)
    subprocess.run(['git','config','user.name','Test'],cwd=workspace,check=True)
    subprocess.run(['git','add','.'],cwd=workspace,check=True); subprocess.run(['git','commit','-m','initial'],cwd=workspace,check=True,stdout=subprocess.DEVNULL)
    plan={'summary':'Proposal only','milestones':[{'title':'Inspect','objective':'Inspect current behavior','acceptance_criteria':['Evidence captured']}]}
    fake=tmp_path/'codex'; fake.write_text('#!/bin/sh\nprintf \'%s\\n\' \''+json.dumps({'item':{'type':'agent_message','text':json.dumps(plan)}}).replace("'", "'\\''")+'\'\n'); fake.chmod(0o755)
    db=Database(tmp_path/'state.db'); db.open()
    db.execute("INSERT INTO workers(id,name,worker_type,config_json) VALUES('w','Planner','managed_planning',?)",(json.dumps({'executable':str(fake),'timeout_seconds':20}),))
    db.execute("INSERT INTO environment_targets(id,name,target_type,config_json) VALUES('e','Repo','workspace',?)",(json.dumps({'workspace_path':str(workspace),'allowed_worker_types':['managed_planning']}),))
    boundary=WorkerExecutionBoundary(db); boundary.register(ManagedPlanningWorkerExecutor())
    return db,boundary,plan


def fake_codex(monkeypatch, plan):
    class Process:
        returncode=0
        async def communicate(self):
            event={'item':{'type':'agent_message','text':json.dumps(plan)}}
            return (json.dumps(event).encode()+b'\n', b'')
    async def launch(*args, **kwargs): return Process()
    monkeypatch.setattr('virtizai_core.workers.asyncio.create_subprocess_exec', launch)


def planning_milestones(count):
    return [
        {
            'title': f'Milestone {index}',
            'objective': f'Complete bounded objective {index}',
            'acceptance_criteria': [f'Objective {index} is verified'],
        }
        for index in range(1, count + 1)
    ]


def test_managed_planning_accepts_six_milestones():
    plan = {'summary': 'Bounded plan', 'milestones': planning_milestones(6)}
    assert ManagedPlanningWorkerExecutor._validate_plan(plan) == plan


def test_managed_planning_rejects_seven_milestones():
    plan = {'summary': 'Oversized plan', 'milestones': planning_milestones(7)}
    with pytest.raises(WorkerExecutionError, match='invalid JSON plan'):
        ManagedPlanningWorkerExecutor._validate_plan(plan)


def test_managed_planning_output_schema_matches_validator_bounds():
    schema = ManagedPlanningWorkerExecutor._output_schema()
    assert schema['required'] == ['summary', 'milestones']
    assert schema['additionalProperties'] is False
    assert set(schema['properties']) == {'summary', 'milestones'}
    assert schema['properties']['summary'] == {'type': 'string', 'minLength': 1, 'maxLength': 1000, 'pattern': r'\S'}
    milestones = schema['properties']['milestones']
    assert (milestones['minItems'], milestones['maxItems']) == (1, 6)
    item = milestones['items']
    assert item['required'] == ['title', 'objective', 'acceptance_criteria']
    assert item['additionalProperties'] is False
    assert set(item['properties']) == {'title', 'objective', 'acceptance_criteria'}
    assert item['properties']['title'] == {'type': 'string', 'minLength': 1, 'maxLength': 160, 'pattern': r'\S'}
    assert item['properties']['objective'] == {'type': 'string', 'minLength': 1, 'maxLength': 1200, 'pattern': r'\S'}
    criteria = item['properties']['acceptance_criteria']
    assert (criteria['minItems'], criteria['maxItems']) == (1, 6)
    assert criteria['items'] == {'type': 'string', 'minLength': 1, 'maxLength': 300, 'pattern': r'\S'}


@pytest.mark.asyncio
async def test_managed_planning_role_read_only_json_contract(tmp_path, monkeypatch):
    db,boundary,plan=setup_planner(tmp_path)
    launched = {}
    class Process:
        returncode=0
        async def communicate(self):
            event={'item':{'type':'agent_message','text':json.dumps(plan)}}
            return (json.dumps(event).encode()+b'\n', b'')
    async def launch(*args, **kwargs):
        launched['args'] = args
        schema_path = Path(args[args.index('--output-schema') + 1])
        launched['schema_path'] = schema_path
        launched['schema'] = json.loads(schema_path.read_text())
        return Process()
    monkeypatch.setattr('virtizai_core.workers.asyncio.create_subprocess_exec', launch)
    denied=await boundary.execute(ExecutionRequest('w','e','managed_planning',{'role_id':'role-coding','objective':'plan'}))
    result=await boundary.execute(ExecutionRequest('w','e','managed_planning',{'role_id':'role-project-lead','objective':'plan'}))
    assert denied.status=='failed' and 'role-project-lead' in denied.error_summary
    assert result.status=='succeeded' and result.output['sandbox']=='read-only' and result.output['plan']==plan, result.error_summary
    assert '--json' in launched['args'] and '--sandbox' in launched['args']
    assert launched['args'][launched['args'].index('--sandbox') + 1] == 'read-only'
    assert launched['schema'] == ManagedPlanningWorkerExecutor._output_schema()
    assert launched['schema_path'].parent == Path('/tmp')
    assert not launched['schema_path'].exists()
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['error', 'timeout'])
async def test_managed_planning_cleans_output_schema_on_failure(tmp_path, monkeypatch, outcome):
    db,boundary,_=setup_planner(tmp_path)
    launched = {}
    class Process:
        returncode = 1 if outcome == 'error' else None
        calls = 0
        async def communicate(self):
            self.calls += 1
            if outcome == 'timeout' and self.calls == 1:
                raise asyncio.TimeoutError
            return b'', b'failed'
        def kill(self):
            self.returncode = -9
    async def launch(*args, **kwargs):
        launched['schema_path'] = Path(args[args.index('--output-schema') + 1])
        assert launched['schema_path'].is_file()
        return Process()
    monkeypatch.setattr('virtizai_core.workers.asyncio.create_subprocess_exec', launch)
    result=await boundary.execute(ExecutionRequest('w','e','managed_planning',{'role_id':'role-project-lead','objective':'plan'}, timeout_seconds=0.01))
    assert result.status == 'failed'
    assert not launched['schema_path'].exists()
    db.close()


@pytest.mark.asyncio
async def test_phase2a_persists_named_assignment_and_plan_without_execution(tmp_path, monkeypatch):
    db,boundary,plan=setup_planner(tmp_path); fake_codex(monkeypatch, plan)
    db.execute("INSERT INTO users(id,display_name) VALUES('u','U')"); db.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')")
    db.execute("INSERT INTO providers(id,name,adapter_type) VALUES('p','Cloud','mock')"); db.execute("INSERT INTO models(id,provider_id,name,locality) VALUES('m','p','M','remote')")
    delegation=type('Delegation',(),{'workers':boundary})(); service=ProjectLeadService(db,None,delegation,lambda role:{})
    manager=dict(db.fetch_one("SELECT id,name FROM project_managers WHERE name='Sarah'"))
    project=await service.run('s','Plan a feature',{'provider_id':'p','model_id':'m','worker_id':'w','environment_id':'e'},manager,'u','discord')
    assert project['status']=='planned' and project['assignment']['project_manager_name']=='Sarah', project.get('blocker_summary')
    assert project['plans'][0]['execution_contract']=='managed_planning' and project['plans'][0]['sandbox']=='read-only'
    assert db.fetch_one('SELECT COUNT(*) FROM project_assignment_audit')[0]==2
    assert db.fetch_one('SELECT COUNT(*) FROM jobs')[0]==0 and db.fetch_one('SELECT COUNT(*) FROM project_milestones')[0]==0
    db.close()
