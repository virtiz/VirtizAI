"""Bounded, durable Project/Lead Agent orchestration.

The lead is an ordinary configured Agent role.  It can only plan and review;
specialist execution remains in the existing DelegationService boundary.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .orchestration import AgentWorkRequest, DelegationService


@dataclass(frozen=True)
class ProjectLimits:
    max_milestones: int = 6
    max_active_children: int = 1
    max_revisions: int = 1
    max_lead_inferences: int = 12
    max_evidence_bytes: int = 4000


class ProjectLeadError(ValueError):
    pass


class ProjectLeadService:
    """Create, execute, and inspect a sequential project plan."""

    def __init__(self, database, providers, delegation: DelegationService,
                 resolve_execution: Callable[[str], dict], limits: ProjectLimits | None = None) -> None:
        self.database = database
        self.providers = providers
        self.delegation = delegation
        self.resolve_execution = resolve_execution
        self.limits = limits or ProjectLimits()

    @staticmethod
    def _tool(name: str, parameters: dict) -> dict:
        return {"type": "function", "function": {"name": name, "description": "Return only this structured decision.", "parameters": parameters}}

    @classmethod
    def _plan_tools(cls) -> list[dict]:
        milestone = {
            "type": "object", "additionalProperties": False,
                "required": ["title", "objective", "acceptance_criteria", "specialist_role_id"],
            "properties": {
                "title": {"type": "string", "maxLength": 160},
                "objective": {"type": "string", "maxLength": 1200},
                "acceptance_criteria": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string", "maxLength": 300}},
                "specialist_role_id": {"type": "string", "enum": ["role-coding", "role-infrastructure"]},
            },
        }
        return [cls._tool("plan_project", {"type": "object", "additionalProperties": False,
                 "required": ["milestones"], "properties": {"milestones": {"type": "array", "minItems": 1, "maxItems": 6, "items": milestone}}})]

    @classmethod
    def _review_tools(cls) -> list[dict]:
        return [cls._tool("review_milestone", {"type": "object", "additionalProperties": False,
                 "required": ["decision", "summary"], "properties": {
                     "decision": {"type": "string", "enum": ["ACCEPT_MILESTONE", "REVISE_MILESTONE", "BLOCK_PROJECT"]},
                     "summary": {"type": "string", "maxLength": 600},
                     "revised_objective": {"type": "string", "maxLength": 1200},
                 }})]

    @staticmethod
    def _call(response, expected: str) -> dict[str, Any]:
        calls = getattr(response, "tool_calls", ())
        if len(calls) != 1:
            raise ProjectLeadError("Project Lead returned invalid structured output")
        function = calls[0].get("function") if isinstance(calls[0], dict) else None
        if not isinstance(function, dict) or function.get("name") != expected:
            raise ProjectLeadError("Project Lead returned invalid structured output")
        args = function.get("arguments")
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except json.JSONDecodeError as exc:
            raise ProjectLeadError("Project Lead returned invalid structured output") from exc
        if not isinstance(args, dict):
            raise ProjectLeadError("Project Lead returned invalid structured output")
        return args

    def _increment(self, project_id: str) -> None:
        self.database.execute("UPDATE projects SET lead_inference_count=lead_inference_count+1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (project_id,))
        row = self.database.fetch_one("SELECT lead_inference_count FROM projects WHERE id=?", (project_id,))
        if row is None or row["lead_inference_count"] > self.limits.max_lead_inferences:
            raise ProjectLeadError("Project Lead inference limit exceeded")

    async def _infer(self, project_id: str, selection: dict, messages: list[dict], tools: list[dict]):
        self._increment(project_id)
        model = self.database.fetch_one("SELECT name FROM models WHERE id=?", (selection["model_id"],))
        if model is None:
            raise ProjectLeadError("Project Lead model is unavailable")
        return await self.providers.chat(selection["provider_id"], model["name"], messages, max_tokens=384, tools=tools, tool_choice="required")

    def _create(self, session_id: str, objective: str, selection: dict) -> str:
        project_id = str(uuid.uuid4())
        self.database.execute(
            """INSERT INTO projects(id,name,objective,status,originating_session_id,lead_role_id,lead_provider_id,lead_model_id)
               VALUES (?,?,?,'planning',?,?,?,?)""",
            (project_id, objective[:160], objective[:2000], session_id, "role-project-lead", selection["provider_id"], selection["model_id"]),
        )
        return project_id

    def _persist_plan(self, project_id: str, milestones: Any) -> list[dict]:
        if not isinstance(milestones, list) or not 1 <= len(milestones) <= self.limits.max_milestones:
            raise ProjectLeadError("Project plan exceeds milestone limit")
        parsed: list[dict] = []
        for ordinal, item in enumerate(milestones, 1):
            if not isinstance(item, dict) or set(item) != {"title", "objective", "acceptance_criteria", "specialist_role_id"}:
                raise ProjectLeadError("Project Lead returned invalid milestone")
            title, objective, criteria, specialist = item["title"], item["objective"], item["acceptance_criteria"], item["specialist_role_id"]
            if not isinstance(title, str) or not title.strip() or len(title) > 160 or not isinstance(objective, str) or not objective.strip() or len(objective) > 1200 or specialist not in {"role-coding", "role-infrastructure"} or not isinstance(criteria, list) or not 1 <= len(criteria) <= 6 or not all(isinstance(c, str) and c and len(c) <= 300 for c in criteria):
                raise ProjectLeadError("Project Lead returned invalid milestone")
            milestone_id = str(uuid.uuid4())
            self.database.execute("""INSERT INTO project_milestones(id,project_id,ordinal,title,objective,specialist_role_id,acceptance_criteria_json)
                VALUES (?,?,?,?,?,?,?)""", (milestone_id, project_id, ordinal, title, objective, specialist, json.dumps(criteria)))
            parsed.append({"id": milestone_id, "ordinal": ordinal, "title": title, "objective": objective, "specialist_role_id": specialist, "acceptance_criteria": criteria})
        self.database.execute("UPDATE projects SET status='running',current_milestone_ordinal=1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (project_id,))
        return parsed

    def _evidence(self, job: dict) -> dict:
        raw = json.loads(job.get("result_json") or "{}")
        output = raw.get("output") if isinstance(raw.get("output"), dict) else {}
        trace = raw.get("trace") if isinstance(raw.get("trace"), list) else output.get("trace", [])
        return {"status": job.get("status"), "operations": [{"operation": t.get("operation"), "status": t.get("status")} for t in trace[:3] if isinstance(t, dict)], "summary": str(output.get("final_summary") or job.get("result_summary") or job.get("error_summary") or "")[:600], "rejection_code": (raw.get("rejection_diagnostic") or {}).get("rejection_code") if isinstance(raw.get("rejection_diagnostic"), dict) else None}

    async def run(self, session_id: str, objective: str, selection: dict) -> dict:
        project_id = self._create(session_id, objective, selection)
        try:
            plan_response = await self._infer(project_id, selection, [
                {"role": "system", "content": "You are the configured Project Lead. Plan only bounded sequential work. Use role-coding for repository work and role-infrastructure only for a configured bounded infrastructure objective. Return one plan_project native function call; no prose plans."},
                {"role": "user", "content": objective[:2000]},
            ], self._plan_tools())
            plan = self._call(plan_response, "plan_project")
            milestones = self._persist_plan(project_id, plan.get("milestones"))
            for milestone in milestones:
                coding = self.resolve_execution(milestone["specialist_role_id"])
                current_objective = milestone["objective"]
                while True:
                    active = self.database.fetch_one("SELECT COUNT(*) AS n FROM project_milestones WHERE project_id=? AND status='running'", (project_id,))["n"]
                    if active >= self.limits.max_active_children:
                        raise ProjectLeadError("Project already has an active child job")
                    self.database.execute("UPDATE project_milestones SET status='running',updated_at=CURRENT_TIMESTAMP WHERE id=?", (milestone["id"],))
                    child = await self.delegation.delegate_agent(AgentWorkRequest(session_id, milestone["specialist_role_id"], coding["provider_id"], coding["model_id"], coding["worker_id"], coding["environment_id"], current_objective, project_id=project_id))
                    evidence = self._evidence(child)
                    self.database.execute("UPDATE project_milestones SET job_id=?, evidence_json=?, result_summary=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (child["id"], json.dumps(evidence)[:self.limits.max_evidence_bytes], evidence["summary"], "reviewing" if child.get("status") == "succeeded" else "failed", milestone["id"]))
                    if child.get("status") != "succeeded":
                        raise ProjectLeadError("Child milestone failed")
                    review_prompt = {"milestone": {"title": milestone["title"], "objective": current_objective, "acceptance_criteria": milestone["acceptance_criteria"]}, "evidence": evidence}
                    review = self._call(await self._infer(project_id, selection, [{"role": "system", "content": "Review bounded child evidence. Return review_milestone only. Accept only when criteria are met; one revision maximum."}, {"role": "user", "content": json.dumps(review_prompt, separators=(",", ":"))[:4000]}], self._review_tools()), "review_milestone")
                    if set(review) - {"decision", "summary", "revised_objective"} or not isinstance(review.get("summary"), str):
                        raise ProjectLeadError("Project Lead returned invalid review")
                    choice = review.get("decision")
                    if choice == "ACCEPT_MILESTONE":
                        self.database.execute("UPDATE project_milestones SET status='succeeded',result_summary=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (review["summary"][:600], milestone["id"]))
                        break
                    if choice == "REVISE_MILESTONE":
                        if not isinstance(review.get("revised_objective"), str) or not review["revised_objective"].strip() or len(review["revised_objective"]) > 1200:
                            raise ProjectLeadError("Project Lead returned invalid revision")
                        row = self.database.fetch_one("SELECT revision_count FROM project_milestones WHERE id=?", (milestone["id"],))
                        if row["revision_count"] >= self.limits.max_revisions:
                            raise ProjectLeadError("Project Lead exceeded milestone revision limit")
                        current_objective = review["revised_objective"]
                        self.database.execute("UPDATE project_milestones SET revision_count=revision_count+1,objective=?,status='pending',updated_at=CURRENT_TIMESTAMP WHERE id=?", (current_objective, milestone["id"]))
                        continue
                    if choice == "BLOCK_PROJECT":
                        raise ProjectLeadError(review["summary"][:600] or "Project blocked by Lead review")
                    raise ProjectLeadError("Project Lead returned invalid review")
                self.database.execute("UPDATE projects SET current_milestone_ordinal=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (milestone["ordinal"] + 1, project_id))
            summary = "Project completed: " + "; ".join(item["title"] for item in milestones)
            self.database.execute("UPDATE projects SET status='succeeded',completion_summary=?,current_milestone_ordinal=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (summary[:1000], project_id))
        except Exception as exc:
            message = str(exc)[:600] or "Project execution failed"
            self.database.execute("UPDATE projects SET status='blocked',blocker_summary=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (message, project_id))
        return self.get(project_id) or {"id": project_id, "status": "blocked"}

    def get(self, project_id: str) -> dict | None:
        project = self.database.fetch_one("SELECT * FROM projects WHERE id=?", (project_id,))
        if project is None:
            return None
        result = dict(project)
        result["milestones"] = [dict(row) for row in self.database.fetch_all("SELECT * FROM project_milestones WHERE project_id=? ORDER BY ordinal", (project_id,))]
        return result
