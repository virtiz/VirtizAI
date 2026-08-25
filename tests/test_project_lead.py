import json
from types import SimpleNamespace

import pytest

from virtizai_core.db import Database
from virtizai_core.project_lead import ProjectLeadService


def call(name, arguments):
    return SimpleNamespace(tool_calls=({"type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},))


class Provider:
    def __init__(self, replies): self.replies = list(replies); self.calls = []
    async def chat(self, *args, **kwargs): self.calls.append(kwargs); return self.replies.pop(0)


class Delegation:
    def __init__(self, db): self.db = db; self.requests = []; self.n = 0
    async def delegate_agent(self, request):
        self.requests.append(request); self.n += 1
        self.db.execute("INSERT INTO jobs(id,user_id,session_id,project_id,kind,status,payload_json) VALUES(?,?,?,?,?,'succeeded','{}')", (f"job-{self.n}", "u", request.session_id, request.project_id, "delegated_agent"))
        return {"id": f"job-{self.n}", "status": "succeeded", "result_json": json.dumps({"trace": [{"operation": "inspect_file", "status": "succeeded"}], "output": {"final_summary": "inspected"}}), "result_summary": "inspected"}


def setup(tmp_path, replies):
    db = Database(tmp_path / "project.db"); db.open()
    db.execute("INSERT INTO users(id,display_name) VALUES('u','U')")
    db.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')")
    db.execute("INSERT INTO providers(id,name,adapter_type) VALUES('p','P','mock')")
    db.execute("INSERT INTO models(id,provider_id,name) VALUES('m','p','M')")
    db.execute("UPDATE roles SET enabled=1 WHERE id IN ('role-coding','role-project-lead')")
    provider, delegation = Provider(replies), Delegation(db)
    resolve = lambda role: {"provider_id": "p", "model_id": "m", "worker_id": "w", "environment_id": "e"}
    return db, ProjectLeadService(db, provider, delegation, resolve), provider, delegation


@pytest.mark.asyncio
async def test_project_plan_child_job_and_acceptance_are_durable(tmp_path):
    plan = {"milestones": [{"title": "Inspect", "objective": "Inspect README.md", "acceptance_criteria": ["Read file"], "specialist_role_id": "role-coding"}]}
    db, service, provider, delegation = setup(tmp_path, [call("plan_project", plan), call("review_milestone", {"decision": "ACCEPT_MILESTONE", "summary": "criteria met"})])
    project = await service.run("s", "Plan a multi-step repository task", {"provider_id": "p", "model_id": "m"})
    assert project["status"] == "succeeded"
    assert project["lead_role_id"] == "role-project-lead"
    assert len(project["milestones"]) == 1 and project["milestones"][0]["status"] == "succeeded"
    assert delegation.requests[0].role_id == "role-coding" and delegation.requests[0].project_id == project["id"]
    assert project["milestones"][0]["job_id"] == "job-1"
    assert len(project["milestones"][0]["evidence_json"]) <= service.limits.max_evidence_bytes
    assert provider.calls[0]["tool_choice"] == "required"
    db.close()


@pytest.mark.asyncio
async def test_malformed_plan_blocks_safely(tmp_path):
    db, service, _, _ = setup(tmp_path, [call("plan_project", {"milestones": []})])
    project = await service.run("s", "project", {"provider_id": "p", "model_id": "m"})
    assert project["status"] == "blocked" and "milestone limit" in project["blocker_summary"]
    db.close()


@pytest.mark.asyncio
async def test_one_revision_is_executed_and_second_is_blocked(tmp_path):
    plan = {"milestones": [{"title": "Inspect", "objective": "Inspect README.md", "acceptance_criteria": ["Read"], "specialist_role_id": "role-coding"}]}
    replies = [call("plan_project", plan), call("review_milestone", {"decision": "REVISE_MILESTONE", "summary": "need focused retry", "revised_objective": "Inspect README.md again"}), call("review_milestone", {"decision": "ACCEPT_MILESTONE", "summary": "now met"})]
    db, service, _, delegation = setup(tmp_path, replies)
    project = await service.run("s", "project", {"provider_id": "p", "model_id": "m"})
    assert project["status"] == "succeeded" and len(delegation.requests) == 2
    assert project["milestones"][0]["revision_count"] == 1
    db.close()


@pytest.mark.asyncio
async def test_second_revision_is_blocked(tmp_path):
    plan = {"milestones": [{"title": "Inspect", "objective": "Inspect README.md", "acceptance_criteria": ["Read"], "specialist_role_id": "role-coding"}]}
    replies = [call("plan_project", plan), call("review_milestone", {"decision": "REVISE_MILESTONE", "summary": "retry", "revised_objective": "again"}), call("review_milestone", {"decision": "REVISE_MILESTONE", "summary": "retry", "revised_objective": "again"})]
    db, service, _, _ = setup(tmp_path, replies)
    project = await service.run("s", "project", {"provider_id": "p", "model_id": "m"})
    assert project["status"] == "blocked" and "revision limit" in project["blocker_summary"]
    db.close()
