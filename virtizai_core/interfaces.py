from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from .db import Database
from .services import CoreService, SecretaryResponse
from .orchestration import AgentWorkRequest, DelegationService
from .policy import CommunicationPolicy, normalize_policy
from .delegation_policy import DelegationPolicyEngine
from .project_lead import ProjectLeadService
from .routing import RoutingEngine


@dataclass(frozen=True)
class InterfaceRequest:
    interface_type: str
    external_subject: str
    content: str
    session_key: str | None = None
    session_id: str | None = None
    display_name: str = "User"
    response_verbosity: str | None = None
    execution_updates: str | None = None
    tool_details: str | None = None


class InterfaceService:
    """Single normalization boundary shared by WebUI, Discord, and CLI."""

    def __init__(self, database: Database, core: CoreService, delegation: DelegationService | None = None) -> None:
        self.database = database
        self.core = core
        self.delegation = delegation
        self.delegation_policy = DelegationPolicyEngine(database, getattr(core, "providers", None), getattr(core, "_secretary_candidates", None))
        self.project_lead = ProjectLeadService(database, getattr(core, "providers", None), delegation, self._delegated_execution) if delegation is not None else None

    def resolve_user(self, interface_type: str, external_subject: str, display_name: str = "User") -> str:
        row = self.database.fetch_one("SELECT user_id FROM interface_identities WHERE interface_type = ? AND external_subject = ?", (interface_type, external_subject))
        if row:
            return row["user_id"]
        user_id = str(uuid.uuid4())
        self.core.sessions.ensure_user(user_id, display_name)
        self.database.execute("INSERT INTO interface_identities(id, user_id, interface_type, external_subject, display_name) VALUES (?, ?, ?, ?, ?)", (str(uuid.uuid4()), user_id, interface_type, external_subject, display_name))
        return user_id

    def link_identity(self, interface_type: str, external_subject: str, user_id: str, display_name: str = "User") -> None:
        if self.database.fetch_one("SELECT id FROM users WHERE id = ?", (user_id,)) is None:
            raise LookupError("VirtizAI user not found")
        self.database.execute("INSERT INTO interface_identities(id, user_id, interface_type, external_subject, display_name) VALUES (?, ?, ?, ?, ?) ON CONFLICT(interface_type, external_subject) DO UPDATE SET user_id=excluded.user_id, display_name=excluded.display_name", (str(uuid.uuid4()), user_id, interface_type, external_subject, display_name))

    def resolve_session(self, request: InterfaceRequest) -> str:
        user_id = self.resolve_user(request.interface_type, request.external_subject, request.display_name)
        if request.session_id:
            owned = self.database.fetch_one("SELECT id FROM sessions WHERE id = ? AND user_id = ?", (request.session_id, user_id))
            if not owned:
                raise PermissionError("Session does not belong to mapped interface user")
            return request.session_id
        key = request.session_key or f"{request.interface_type}:{request.external_subject}"
        row = self.database.fetch_one("SELECT session_id FROM interface_sessions WHERE interface_type = ? AND external_session_key = ? AND user_id = ?", (request.interface_type, key, user_id))
        if row:
            return row["session_id"]
        session_id = self.core.sessions.create_session(user_id)
        self.database.execute("INSERT INTO interface_sessions(interface_type, external_session_key, session_id, user_id) VALUES (?, ?, ?, ?)", (request.interface_type, key, session_id, user_id))
        return session_id

    async def handle(self, request: InterfaceRequest) -> tuple[str, SecretaryResponse]:
        user_id = self.resolve_user(request.interface_type, request.external_subject, request.display_name)
        session_id = self.resolve_session(request)
        base = self.database.fetch_one("SELECT response_verbosity, execution_updates, tool_details FROM communication_preferences WHERE user_id = ?", (user_id,))
        override = self.database.fetch_one("SELECT response_verbosity, execution_updates, tool_details FROM interface_preferences WHERE user_id = ? AND interface_type = ?", (user_id, request.interface_type))
        inherited = dict(override or base) if (override or base) else None
        policy = normalize_policy(request.response_verbosity, request.execution_updates, request.tool_details, CommunicationPolicy(**inherited) if inherited else None)
        decision = await self.delegation_policy.decide(request.content)
        if decision.decision == "delegate" and self.delegation is not None:
            if decision.role_id == "role-project-lead":
                session_id, response = await self.project_for_session(request, decision.objective)
            else:
                session_id, response = await self.delegate_for_session(request, decision.role_id or "", decision.objective)
            self.database.execute("INSERT INTO interface_events(interface_type,user_id,session_id,event_type,metadata_json) VALUES (?,?,?,?,?)", (request.interface_type, user_id, session_id, "delegation_decision", json.dumps(decision.metadata())))
            return session_id, response
        response = await self.core.handle_message(
            user_id, session_id, request.content, request.display_name, policy,
            request.interface_type,
            {"interface": request.interface_type, "external_subject": request.external_subject},
        )
        self.database.execute("INSERT INTO interface_events(interface_type, user_id, session_id, event_type, metadata_json) VALUES (?, ?, ?, 'message', ?)", (request.interface_type, user_id, session_id, json.dumps({"request_id": response.request_id})))
        return session_id, response

    def _delegated_execution(self, role_id: str) -> dict:
        decision = RoutingEngine(self.database).capability_selection(role_id)
        selected = decision.get("selected")
        if selected is None: raise LookupError("No capability-eligible delegated execution target is configured")
        row = self.database.fetch_one("SELECT policy_json FROM routes WHERE id=?", (selected["route_id"],))
        if row is None:
            raise LookupError("No delegated execution route is configured for agent")
        try:
            policy = json.loads(row["policy_json"] or "{}")
        except json.JSONDecodeError:
            policy = {}
        execution = policy.get("delegated_execution") if isinstance(policy, dict) else None
        if not isinstance(execution, dict) or not all(isinstance(execution.get(key), str) and execution[key] for key in ("worker_id", "environment_id")):
            raise LookupError("Delegated execution route is incomplete")
        return {"provider_id": selected["provider_id"], "model_id": selected["model_id"], "worker_id": execution["worker_id"], "environment_id": execution["environment_id"], "routing_decision": decision, "fallback": decision.get("fallback_candidates", [])[:1]}

    async def delegate_for_session(self, request: InterfaceRequest, role_id: str, objective: str) -> tuple[str, SecretaryResponse]:
        if self.delegation is None:
            raise RuntimeError("Generic delegation is not configured")
        user_id = self.resolve_user(request.interface_type, request.external_subject, request.display_name)
        session_id = self.resolve_session(request)
        role = self.database.fetch_one("SELECT id,enabled FROM roles WHERE id=?", (role_id,))
        if role is None or not role["enabled"]:
            raise LookupError("Delegated agent is unavailable")
        selection = self._delegated_execution(role_id)
        job = await self.delegation.delegate_agent(AgentWorkRequest(session_id, role_id, selection["provider_id"], selection["model_id"], selection["worker_id"], selection["environment_id"], objective, context={"routing_decision": selection["routing_decision"]}))
        first = json.loads(job.get("result_json") or "{}")
        trace = first.get("trace") if isinstance(first.get("trace"), list) else []
        if job.get("status") != "succeeded" and not trace and selection["fallback"]:
            fallback = selection["fallback"][0]
            decision = {**selection["routing_decision"], "fallback_used": True, "fallback_reason": "pre_tool_primary_failure", "selected": fallback}
            job = await self.delegation.delegate_agent(AgentWorkRequest(session_id, role_id, fallback["provider_id"], fallback["model_id"], selection["worker_id"], selection["environment_id"], objective, context={"routing_decision": decision}))
        result = json.loads(job.get("result_json") or "{}")
        output = result.get("output") if isinstance(result.get("output"), dict) else {}
        content = str(output.get("final_summary") or job.get("error_summary") or job.get("result_summary") or f"Delegated job {job.get('status')}")[:1800]
        message = self.database.fetch_one("SELECT id FROM messages WHERE session_id=? AND metadata_json LIKE ? ORDER BY created_at DESC LIMIT 1", (session_id, f'%{job["id"]}%'))
        response = SecretaryResponse(str(uuid.uuid4()), session_id, message["id"] if message else "", content, None, selection["provider_id"], selection["model_id"], job_created=True, task_class="delegated")
        self.database.execute("INSERT INTO interface_events(interface_type,user_id,session_id,event_type,metadata_json) VALUES (?,?,?,?,?)", (request.interface_type, user_id, session_id, "delegated_agent", json.dumps({"job_id": job["id"], "role_id": role_id})))
        return session_id, response

    async def project_for_session(self, request: InterfaceRequest, objective: str) -> tuple[str, SecretaryResponse]:
        if self.project_lead is None:
            raise RuntimeError("Project Lead orchestration is not configured")
        user_id = self.resolve_user(request.interface_type, request.external_subject, request.display_name)
        session_id = self.resolve_session(request)
        role = self.database.fetch_one("SELECT id,enabled FROM roles WHERE id='role-project-lead'")
        if role is None or not role["enabled"]:
            raise LookupError("Project Lead agent is unavailable")
        selection = self._lead_execution("role-project-lead")
        project = await self.project_lead.run(session_id, objective, selection)
        status = project.get("status")
        content = str(project.get("completion_summary") or project.get("blocker_summary") or f"Project {status}")[:1800]
        self.core.sessions.add_message(session_id, "assistant", content, {"execution_type": "project_lead", "project_id": project["id"], "role_id": "role-project-lead", "status": status})
        message = self.database.fetch_one("SELECT id FROM messages WHERE session_id=? AND metadata_json LIKE ? ORDER BY created_at DESC LIMIT 1", (session_id, f'%{project["id"]}%'))
        response = SecretaryResponse(str(uuid.uuid4()), session_id, message["id"] if message else "", content, None, selection["provider_id"], selection["model_id"], job_created=True, task_class="project")
        self.database.execute("INSERT INTO interface_events(interface_type,user_id,session_id,event_type,metadata_json) VALUES (?,?,?,?,?)", (request.interface_type, user_id, session_id, "project_result", json.dumps({"project_id": project["id"], "status": status, "lead_role_id": "role-project-lead"})))
        return session_id, response

    def _lead_execution(self, role_id: str) -> dict:
        decision = RoutingEngine(self.database).capability_selection(role_id)
        row = decision.get("selected")
        if row is None:
            raise LookupError("No Project Lead route is configured")
        return {"provider_id": row["provider_id"], "model_id": row["model_id"], "routing_decision": decision}

    def history(self, interface_type: str, external_subject: str, session_id: str | None = None) -> list[dict]:
        user_id = self.resolve_user(interface_type, external_subject)
        if session_id:
            owned = self.database.fetch_one("SELECT id FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
            if not owned:
                raise PermissionError("Session does not belong to mapped interface user")
        else:
            row = self.database.fetch_one("SELECT session_id FROM interface_sessions WHERE interface_type = ? AND user_id = ? ORDER BY created_at DESC LIMIT 1", (interface_type, user_id))
            session_id = row["session_id"] if row else None
        if not session_id:
            return []
        return [dict(row) for row in self.database.fetch_all("SELECT * FROM messages WHERE session_id = ? ORDER BY created_at", (session_id,))]
