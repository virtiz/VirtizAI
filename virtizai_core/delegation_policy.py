"""Bounded Secretary-to-specialist delegation policy."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationDecision:
    decision: str
    role_id: str | None
    confidence: float | None
    reason_code: str
    source: str
    objective: str
    legacy_fallback: bool = False

    def metadata(self) -> dict:
        return {"decision_source": self.source, "selected_role_id": self.role_id, "confidence": self.confidence, "reason_code": self.reason_code, "legacy_fallback": self.legacy_fallback}


class DelegationPolicyEngine:
    def __init__(self, database, providers=None, secretary_candidates=None, high_confidence: float | None = None) -> None:
        self.database, self.providers, self.secretary_candidates = database, providers, secretary_candidates
        self.high_confidence = max(0.0, min(1.0, high_confidence if high_confidence is not None else float(os.environ.get("VIRTIZAI_DELEGATION_HIGH_CONFIDENCE", "0.85"))))

    def deterministic(self, content: str) -> DelegationDecision | None:
        text = content.strip(); lowered = text.lower()
        if text.startswith("/project ") and text[9:].strip():
            return DelegationDecision("delegate", "role-project-lead", 1.0, "explicit_project", "explicit-user", text[9:].strip())
        if text.startswith("/coding ") and text[8:].strip():
            return DelegationDecision("delegate", "role-coding", 1.0, "explicit_coding", "explicit-user", text[8:].strip())
        multi_step = re.search(r"\b(plan|manage|milestone|multi-step|multiple files|end-to-end|implementation and validation)\b", lowered)
        engineering = re.search(r"\b(implement|build|refactor|feature|release|code|repository|tests?)\b", lowered)
        if multi_step and engineering:
            return DelegationDecision("delegate", "role-project-lead", 1.0, "bounded_project_request", "deterministic", text)
        if re.search(r"\b(edit|modify|fix|implement|patch|change)\b.*\b(code|source|repository|repo|file|module|function|class|test|tests|readme)\b", lowered) or re.search(r"\brun\s+(?:the\s+)?(?:project\s+)?tests?\b", lowered):
            return DelegationDecision("delegate", "role-coding", 1.0, "bounded_coding_request", "deterministic", text)
        if re.search(r"\binspect\b", lowered) and re.search(r"\b[\w./-]+\.(py|js|ts|md|json|ya?ml)\b", lowered):
            return DelegationDecision("delegate", "role-coding", 1.0, "bounded_source_inspection", "deterministic", text)
        return None

    async def decide(self, content: str) -> DelegationDecision:
        decision = self.deterministic(content)
        if decision: return decision
        decision = await self._classify(content)
        return decision or DelegationDecision("direct", None, None, "classifier_unavailable_or_invalid", "model", "")

    async def _classify(self, content: str) -> DelegationDecision | None:
        if self.providers is None or self.secretary_candidates is None: return None
        try:
            candidates = await self.secretary_candidates()
            if not candidates: return None
            tool = {"type":"function","function":{"name":"delegation_decision","parameters":{"type":"object","additionalProperties":False,"required":["decision","role_id","confidence","reason_code"],"properties":{"decision":{"type":"string","enum":["direct","delegate"]},"role_id":{"type":["string","null"]},"confidence":{"type":"number","minimum":0,"maximum":1},"reason_code":{"type":"string","enum":["coding_request","project_request","ordinary_conversation","technical_explanation","ambiguous"]}}}}}
            response = await self.providers.chat(candidates[0].provider_id, candidates[0].model_name, [{"role":"system","content":"Use delegation_decision exactly once. Delegate only clear repository coding work or clear multi-step engineering projects; explanations and ambiguity are direct."},{"role":"user","content":content}], max_tokens=96, tools=[tool])
            if len(response.tool_calls) != 1: return None
            call = response.tool_calls[0].get("function", {})
            if call.get("name") != "delegation_decision": return None
            args = call.get("arguments"); args = json.loads(args) if isinstance(args, str) else args
            if not isinstance(args, dict) or set(args) != {"decision","role_id","confidence","reason_code"}: return None
            choice, role, confidence, reason = args["decision"], args["role_id"], args["confidence"], args["reason_code"]
            if choice not in {"direct","delegate"} or not isinstance(confidence,(int,float)) or not 0 <= confidence <= 1 or reason not in {"coding_request","project_request","ordinary_conversation","technical_explanation","ambiguous"}: return None
            if choice == "direct": return DelegationDecision("direct", None, float(confidence), reason, "model", "") if role is None else None
            if role not in {"role-coding", "role-project-lead"} or confidence < self.high_confidence: return DelegationDecision("direct", None, float(confidence), "classifier_below_threshold", "model", "")
            if self.database.fetch_one("SELECT id FROM roles WHERE id=? AND enabled=1", (role,)) is None: return None
            return DelegationDecision("delegate", role, float(confidence), reason, "model", content)
        except Exception: return None
