from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

from .db import Database
from .services import CoreService, SecretaryResponse
from .orchestration import AgentWorkRequest, DelegationService
from .policy import CommunicationPolicy, normalize_policy
from .delegation_policy import DelegationDecision, DelegationPolicyEngine
from .project_lead import ProjectLeadService
from .routing import RoutingEngine
from .homelab import HomelabFacts
from .work_intake import WorkIntakeClassifier


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
        self.homelab_facts = HomelabFacts(database)
        self.work_intake = WorkIntakeClassifier()
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

    @staticmethod
    def _infrastructure_follow_up(content: str) -> bool:
        """Recognize only unambiguous references to a prior live inventory."""
        normalized = " ".join(content.lower().strip().rstrip("?!.").split())
        return normalized in {
            "list them", "list them for me", "show them", "what are they",
            "which ones", "which ones are running", "give me their names",
            "what hosts are they on",
        }

    def _has_successful_infrastructure_read(self, session_id: str) -> bool:
        """Require durable same-session list_vms evidence before inheriting context."""
        messages = self.database.fetch_all(
            "SELECT metadata_json FROM messages WHERE session_id=? AND role='assistant' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 12",
            (session_id,),
        )
        for message in messages:
            try:
                metadata = json.loads(message["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict) or metadata.get("role_id") != "role-infrastructure" or metadata.get("status") != "succeeded":
                continue
            job_id = metadata.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                continue
            job = self.database.fetch_one("SELECT status,result_json FROM jobs WHERE id=? AND session_id=?", (job_id, session_id))
            if job is None or job["status"] != "succeeded":
                continue
            try:
                result = json.loads(job["result_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            trace = result.get("trace") if isinstance(result, dict) else None
            if isinstance(trace, list) and any(isinstance(item, dict) and item.get("operation") == "list_vms" and item.get("status") == "succeeded" for item in trace):
                return True
        return False

    def _unresolved_infrastructure_follow_up(self, session_id: str) -> SecretaryResponse:
        content = "I need a successful live infrastructure inventory in this conversation before I can list or compare those resources. Please ask me to list your VMs or containers first."
        message_id = self.core.sessions.add_message(session_id, "assistant", content, {"execution_type": "infrastructure_follow_up_clarification"})
        return SecretaryResponse(str(uuid.uuid4()), session_id, message_id, content, None, None, None, task_class="secretary")

    def _named_pm(self, content: str) -> dict | None:
        for manager in self.database.fetch_all("SELECT id,name FROM project_managers WHERE enabled=1 ORDER BY name"):
            if re.search(rf"\b{re.escape(manager['name'])}\b", content, re.IGNORECASE): return dict(manager)
        return None

    def _automatic_pm(self) -> dict:
        row = self.database.fetch_one("SELECT pm.id,pm.name,COUNT(a.id) workload FROM project_managers pm LEFT JOIN project_assignments a ON a.project_manager_id=pm.id AND a.status IN ('assigned','planned') WHERE pm.enabled=1 GROUP BY pm.id,pm.name ORDER BY workload,pm.id LIMIT 1")
        if row is None: raise LookupError("No named Project Manager is available")
        return dict(row)

    @staticmethod
    def _is_pm_status_query(content: str) -> bool:
        lowered = content.lower()
        return bool(re.search(r"\b(status|working on|assigned|projects?|plans?)\b", lowered) and re.search(r"\b(what|show|tell|status|which|how)\b", lowered))

    def _pm_status_response(self, session_id: str, manager: dict) -> SecretaryResponse:
        rows = self.database.fetch_all("SELECT p.name,p.objective,p.status FROM project_assignments a JOIN projects p ON p.id=a.project_id WHERE a.project_manager_id=? ORDER BY a.updated_at DESC LIMIT 8", (manager["id"],))
        content = f"{manager['name']} (Project Manager) has no persisted project assignments." if not rows else "\n".join([f"{manager['name']} (Project Manager) has {len(rows)} persisted project assignment(s):"] + [f"- {row['name']}: {row['status']} — {row['objective']}" for row in rows])
        message_id = self.core.sessions.add_message(session_id,"assistant",content,{"execution_type":"pm_status","project_manager_id":manager["id"]})
        return SecretaryResponse(str(uuid.uuid4()),session_id,message_id,content,None,None,None,task_class="project_status")

    def _homelab_lookup(self, content: str, session_id: str) -> dict | None:
        if self.work_intake.classify(content).intent != "homelab_lookup":
            return None
        normalized = " ".join(re.findall(r"[a-z0-9-]+", content.lower()))
        aliases = [row["alias"] for row in self.database.fetch_all(
            "SELECT alias FROM homelab_aliases ORDER BY length(alias) DESC, alias"
        )]
        matched_aliases = [item for item in aliases if re.search(
            rf"(?<![a-z0-9]){re.escape(item.lower())}(?![a-z0-9])", normalized
        )]
        matched_entities = {}
        for item in matched_aliases:
            result = self.homelab_facts.lookup(item)
            for entity in result.get("entities", []):
                matched_entities[entity["id"]] = entity
        if len(matched_entities) > 1:
            return {
                "status": "ambiguous", "query": normalized,
                "entities": list(matched_entities.values()),
            }
        alias = matched_aliases[0] if matched_aliases else None
        if alias is None and re.search(r"\b(?:it|that|this|that one|this one)\b", normalized):
            alias = "it"
        if alias is None:
            return {"status": "not_found", "query": normalized, "entities": []}

        entity = self.homelab_facts.lookup(alias, session_id=session_id)
        properties = sorted({
            fact["property"] for item in entity.get("entities", []) for fact in item.get("facts", [])
        }, key=len, reverse=True)
        requested_property = self.homelab_facts.requested_property(content, properties)
        return self.homelab_facts.lookup(alias, requested_property, session_id=session_id)

    def _homelab_response(self, session_id: str, lookup: dict) -> SecretaryResponse:
        status = lookup["status"]
        entities = lookup.get("entities", [])
        if status == "found":
            lines = []
            for entity in entities:
                facts = entity.get("facts", [])
                lines.append(f"{entity['name']}: " + "; ".join(
                    f"{fact['property']} = {fact['value']} (source: {fact['evidence']['source']}, line {fact['evidence']['line']})"
                    for fact in facts
                ))
            content = "\n".join(lines)
        elif status == "ambiguous":
            content = "The persisted facts match multiple configured systems: " + ", ".join(
                entity["name"] for entity in entities
            ) + "."
        elif status == "ambiguous_property":
            content = "That property name matches multiple persisted property labels: " + ", ".join(
                lookup.get("property_matches", [])
            ) + "."
        elif status == "property_not_found":
            content = f"No verified data records that property for {entities[0]['name']}."
        else:
            content = "No persisted fact matches that configured-system lookup."
        marker = {"status": status}
        if status == "found" and len(entities) == 1:
            marker["entity_id"] = entities[0]["id"]
        metadata = {"execution_type": "homelab_lookup", "homelab_lookup": marker, "evidence": [
            fact["evidence"] for entity in entities for fact in entity.get("facts", [])
        ]}
        message_id = self.core.sessions.add_message(session_id, "assistant", content, metadata)
        return SecretaryResponse(str(uuid.uuid4()), session_id, message_id, content, None, None, None, task_class="homelab_lookup")

    async def handle(self, request: InterfaceRequest) -> tuple[str, SecretaryResponse]:
        user_id = self.resolve_user(request.interface_type, request.external_subject, request.display_name)
        session_id = self.resolve_session(request)
        base = self.database.fetch_one("SELECT response_verbosity, execution_updates, tool_details FROM communication_preferences WHERE user_id = ?", (user_id,))
        override = self.database.fetch_one("SELECT response_verbosity, execution_updates, tool_details FROM interface_preferences WHERE user_id = ? AND interface_type = ?", (user_id, request.interface_type))
        inherited = dict(override or base) if (override or base) else None
        policy = normalize_policy(request.response_verbosity, request.execution_updates, request.tool_details, CommunicationPolicy(**inherited) if inherited else None)
        lookup = self._homelab_lookup(request.content, session_id)
        if lookup is not None:
            self.core.sessions.add_message(session_id, "user", request.content, {"interface": request.interface_type})
            return session_id, self._homelab_response(session_id, lookup)
        named_pm = self._named_pm(request.content)
        if named_pm and self._is_pm_status_query(request.content): return session_id, self._pm_status_response(session_id, named_pm)
        decision = await self.delegation_policy.decide(request.content)
        if self._infrastructure_follow_up(request.content):
            if self._has_successful_infrastructure_read(session_id):
                decision = DelegationDecision("delegate", "role-infrastructure", 1.0, "bounded_infrastructure_read", "session-context", "list my VM/container infrastructure resources")
            else:
                return session_id, self._unresolved_infrastructure_follow_up(session_id)
        if decision.decision == "delegate" and self.delegation is not None:
            if decision.role_id == "role-project-lead":
                session_id, response = await self.project_for_session(request, decision.objective, named_pm)
            else:
                session_id, response = await self.delegate_for_session(request, decision.role_id or "", decision.objective, decision)
            self.database.execute("INSERT INTO interface_events(interface_type,user_id,session_id,event_type,metadata_json) VALUES (?,?,?,?,?)", (request.interface_type, user_id, session_id, "delegation_decision", json.dumps(decision.metadata())))
            return session_id, response
        response = await self.core.handle_message(
            user_id, session_id, request.content, request.display_name, policy,
            request.interface_type,
            {"interface": request.interface_type, "external_subject": request.external_subject},
        )
        self.database.execute("INSERT INTO interface_events(interface_type, user_id, session_id, event_type, metadata_json) VALUES (?, ?, ?, 'message', ?)", (request.interface_type, user_id, session_id, json.dumps({"request_id": response.request_id})))
        return session_id, response

    def _delegated_execution(self, role_id: str, delegation_decision = None) -> dict:
        routing_decision = RoutingEngine(self.database).capability_selection(role_id, execution_tier=getattr(delegation_decision, "execution_tier", None))
        selected = routing_decision.get("selected")
        if selected is None:
            raise LookupError("No capability-eligible delegated execution target is configured")
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
        # Intent-aware routing: use delegation decision reason_code to select execution profile
        intent_reason_code = getattr(delegation_decision, "reason_code", None) if delegation_decision else None
        selected_execution = dict(
            provider_id=selected["provider_id"],
            model_id=selected["model_id"],
            worker_id=selected.get("worker_id") or execution["worker_id"],
            environment_id=selected.get("environment_id") or execution["environment_id"],
            execution_plan=selected.get("execution_plan", "native_tool_coding"),
            routing_decision=routing_decision,
            fallback=routing_decision.get("fallback_candidates", [])[:1],
        )
        # Try to resolve execution profile based on intent
        execution_profiles = policy.get("delegated_execution_profiles") if isinstance(policy, dict) else None
        if isinstance(execution_profiles, dict) and intent_reason_code in execution_profiles:
            profile = execution_profiles[intent_reason_code]
            if isinstance(profile, dict):
                return {
                    **selected_execution,
                    **profile,
                }
        return selected_execution

    async def delegate_for_session(self, request: InterfaceRequest, role_id: str, objective: str, delegation_decision = None) -> tuple[str, SecretaryResponse]:
        if self.delegation is None:
            raise RuntimeError("Generic delegation is not configured")
        user_id = self.resolve_user(request.interface_type, request.external_subject, request.display_name)
        session_id = self.resolve_session(request)
        role = self.database.fetch_one("SELECT id,enabled FROM roles WHERE id=?", (role_id,))
        if role is None or not role["enabled"]:
            raise LookupError("Delegated agent is unavailable")
        selection = self._delegated_execution(role_id, delegation_decision)
        job = await self.delegation.delegate_agent(AgentWorkRequest(session_id, role_id, selection["provider_id"], selection["model_id"], selection["worker_id"], selection["environment_id"], objective, context={"routing_decision": selection["routing_decision"], "execution_plan": selection["execution_plan"], "execution_tier": getattr(delegation_decision, "execution_tier", None) or "automatic"}))
        first = json.loads(job.get("result_json") or "{}")
        trace = first.get("trace") if isinstance(first.get("trace"), list) else []
        side_effect_state = DelegationService.side_effect_state(trace)
        protocol_failure = str(first.get("error_summary") or job.get("error_summary") or "") in {"Coding Agent returned malformed tool call", "Coding Agent returned multiple tool calls", "Coding Agent selected an invalid operation", "Delegated provider or execution failed"}
        if job.get("status") != "succeeded" and side_effect_state in {"NO_TOOLS", "READ_ONLY"} and protocol_failure and selection["fallback"]:
            superseded_job_id = job["id"]
            fallback = selection["fallback"][0]
            decision = {**selection["routing_decision"], "fallback_used": True, "fallback_reason": "read_only_protocol_failure", "selected": fallback}
            evidence = [{"operation": item.get("operation"), "status": item.get("status")} for item in trace[:3] if isinstance(item, dict)]
            job = await self.delegation.delegate_agent(AgentWorkRequest(session_id, role_id, fallback["provider_id"], fallback["model_id"], fallback.get("worker_id") or selection["worker_id"], fallback.get("environment_id") or selection["environment_id"], objective, context={"routing_decision": decision, "execution_plan": fallback.get("execution_plan", "native_tool_coding"), "write_authorized": False, "prior_read_evidence": evidence, "prior_failure": str(first.get("error_summary") or job.get("error_summary") or "")[:300]}))
            # The retry is the single response for this request. The synchronous
            # first-attempt failure was persisted internally but never returned.
            self.database.execute(
                "DELETE FROM messages WHERE session_id=? AND role='assistant' AND metadata_json LIKE ?",
                (session_id, f'%"job_id": "{superseded_job_id}"%'),
            )
        result = json.loads(job.get("result_json") or "{}")
        output = result.get("output") if isinstance(result.get("output"), dict) else {}
        content = str(output.get("final_summary") or job.get("error_summary") or job.get("result_summary") or f"Delegated job {job.get('status')}")[:1800]
        message = self.database.fetch_one("SELECT id FROM messages WHERE session_id=? AND metadata_json LIKE ? ORDER BY created_at DESC LIMIT 1", (session_id, f'%{job["id"]}%'))
        response = SecretaryResponse(str(uuid.uuid4()), session_id, message["id"] if message else "", content, None, selection["provider_id"], selection["model_id"], job_created=True, task_class="delegated")
        self.database.execute("INSERT INTO interface_events(interface_type,user_id,session_id,event_type,metadata_json) VALUES (?,?,?,?,?)", (request.interface_type, user_id, session_id, "delegated_agent", json.dumps({"job_id": job["id"], "role_id": role_id})))
        return session_id, response

    async def project_for_session(self, request: InterfaceRequest, objective: str, manager: dict | None = None) -> tuple[str, SecretaryResponse]:
        if self.project_lead is None:
            raise RuntimeError("Project Lead orchestration is not configured")
        user_id = self.resolve_user(request.interface_type, request.external_subject, request.display_name)
        session_id = self.resolve_session(request)
        role = self.database.fetch_one("SELECT id,enabled FROM roles WHERE id='role-project-lead'")
        if role is None or not role["enabled"]:
            raise LookupError("Project Lead agent is unavailable")
        selection = self._lead_execution("role-project-lead")
        manager = manager or self._automatic_pm()
        project = await self.project_lead.run(session_id, objective, selection, manager, user_id, request.interface_type)
        status = project.get("status")
        content = (f"{manager['name']} (Project Manager) is assigned and proposed a persisted plan: {project['plans'][-1]['plan']['summary']} No work has been executed." if status == "planned" and project.get("plans") else f"{manager['name']} (Project Manager): {project.get('blocker_summary') or f'Project {status}'}")[:1800]
        self.core.sessions.add_message(session_id,"assistant",content,{"execution_type":"project_planning","project_id":project["id"],"project_manager_id":manager["id"],"role_id":"role-project-lead","status":status})
        message = self.database.fetch_one("SELECT id FROM messages WHERE session_id=? AND metadata_json LIKE ? ORDER BY created_at DESC LIMIT 1", (session_id, f'%{project["id"]}%'))
        response = SecretaryResponse(str(uuid.uuid4()), session_id, message["id"] if message else "", content, None, selection["provider_id"], selection["model_id"], job_created=False, task_class="project_plan")
        self.database.execute("INSERT INTO interface_events(interface_type,user_id,session_id,event_type,metadata_json) VALUES (?,?,?,?,?)", (request.interface_type, user_id, session_id, "project_result", json.dumps({"project_id": project["id"], "status": status, "lead_role_id": "role-project-lead"})))
        return session_id, response

    def _lead_execution(self, role_id: str) -> dict:
        routing_decision = RoutingEngine(self.database).capability_selection(role_id, execution_tier="cloud")
        row = routing_decision.get("selected")
        if row is None:
            raise LookupError("No eligible remote managed-planning route is configured for Project Lead; planning was not executed")
        route = self.database.fetch_one("SELECT policy_json FROM routes WHERE id=?", (row["route_id"],))
        try: policy = json.loads(route["policy_json"] or "{}") if route else {}
        except json.JSONDecodeError: policy = {}
        execution = policy.get("delegated_execution", {}) if isinstance(policy, dict) else {}
        worker_id, environment_id = row.get("worker_id") or execution.get("worker_id"), row.get("environment_id") or execution.get("environment_id")
        worker = self.database.fetch_one("SELECT worker_type FROM workers WHERE id=?", (worker_id,)) if worker_id else None
        if worker is None or worker["worker_type"] != "managed_planning": raise LookupError("Project Lead requires a managed_planning Codex worker")
        return {"provider_id":row["provider_id"],"model_id":row["model_id"],"worker_id":worker_id,"environment_id":environment_id,"execution_plan":"managed_planning","routing_decision":routing_decision}

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
