from __future__ import annotations

import asyncio
import json
import uuid
import os
import re
import shutil
from pathlib import Path
from dataclasses import dataclass
from time import perf_counter

from .db import Database
from .jobs import JobManager
from .providers import ProviderRegistry
from .routing import RoutingEngine
from .telemetry import TelemetryService
from .policy import CommunicationPolicy
from .workers import TaskClassifier, CodexWorker


@dataclass(frozen=True)
class SecretaryResponse:
    request_id: str
    session_id: str
    message_id: str
    content: str
    route_id: str | None
    provider_id: str | None
    model_id: str | None
    provider_name: str | None = None
    model_name: str | None = None
    latency_ms: float = 0.0
    ttft_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_exact: bool | None = None
    estimated_cost: float | None = None
    job_created: bool = False
    task_class: str = "simple"


class IntrospectionService:
    """Deterministic, secret-free view of configured routes and worker state."""

    _signals = (
        "current routing", "routing configuration", "what model", "what providers",
        "secretary model", "fallback model", "what workers", "codex connected",
        "provider configuration", "configured providers", "why are the local models", "currently offline", "did anything go down", "which one is your secretary fallback", "what was the primary model",
        "which model is responding", "what model answered", "what provider handled", "was that local or cloud",
        "did you use a fallback", "why did you use phi", "did qwen fail", "which agent answered", "what handled the last request",
        "which models are healthy", "why did you fall back", "why can't you use",
        "which providers are down", "status of the medium route", "why are models unavailable",
        "is the secretary model warm", "is secretary warm", "what model is loaded", "which model is loaded",
    )

    def __init__(self, database: Database, workspace_root: Path) -> None:
        self.database = database
        self.workspace_root = workspace_root

    def matches(self, content: str) -> bool:
        text = self._normalized(content)
        return self.is_identity_question(text) or any(self._has_phrase(text, signal) for signal in self._signals)

    @staticmethod
    def _normalized(content: str) -> str:
        """Normalize to words so ``use`` cannot match ``useful`` or ``me`` fragments."""
        return " ".join(re.findall(r"[a-z0-9]+", content.lower()))

    @staticmethod
    def _has_phrase(text: str, phrase: str) -> bool:
        return f" {IntrospectionService._normalized(phrase)} " in f" {text} "

    def identity_kind(self, content: str) -> str | None:
        text = self._normalized(content)
        if ("warm" in text.split() and "secretary" in text.split()) or self._has_phrase(text, "what model is loaded") or self._has_phrase(text, "which model is loaded"):
            return "warm_status"
        if not self.is_identity_question(text):
            return None
        if any(self._has_phrase(text, term) for term in ("last message", "previous response", "that message", "last response", "previous request")):
            return "previous_response"
        if any(self._has_phrase(text, term) for term in ("last inference", "actual model", "last model", "most recent model", "last request")):
            return "last_inference"
        return "current"

    def is_identity_question(self, content: str) -> bool:
        text = self._normalized(content)
        tokens = set(text.split())
        targets = {"model", "provider", "worker", "agent", "route", "inference", "local", "cloud", "codex", "qwen", "phi", "fallback"}
        verbs = {"answer", "answered", "respond", "responded", "responding", "handled", "handle", "use", "used", "using", "generated", "ran", "run"}
        interrogatives = {"what", "which", "who", "did", "was", "were", "is", "are", "can", "does"}
        temporal = any(self._has_phrase(text, phrase) for phrase in ("current response", "right now", "last", "previous", "that", "that message", "last request", "last inference", "most recent"))
        availability = bool(tokens & {"available", "unavailable", "healthy", "offline", "down", "configured", "connected"})
        has_target = bool(tokens & targets)
        has_verb = bool(tokens & verbs)
        question_shape = bool(tokens & interrogatives)
        # Identity requires a question/temporal relationship, not merely words
        # such as "local" and "use" appearing in an ordinary request.
        copula_execution = bool(tokens & {"is", "are", "was", "were"}) and temporal
        return has_target and (((has_verb or copula_execution) and (temporal or question_shape)) or (availability and question_shape))


    def _target(self, row: dict, eligible: set[tuple[str, str]]) -> str:
        key = (row["provider_id"], row["model_id"])
        status = "eligible" if key in eligible else "unavailable"
        return f"{row['provider_name']}:{row['model_name']} ({status})"

    def snapshot(self) -> dict:
        from .routing import RoutingEngine
        roles = {"simple": "secretary", "medium": "general-reasoning"}
        result = {"routes": {}, "providers": [], "workers": []}
        for provider in self.database.fetch_all("SELECT id,name,adapter_type,health_status,enabled FROM providers ORDER BY name"):
            result["providers"].append({
                "name": provider["name"], "type": provider["adapter_type"],
                "health": provider["health_status"], "enabled": bool(provider["enabled"]),
            })
        for task_class, role_name in roles.items():
            role = self.database.fetch_one("SELECT id FROM roles WHERE name=?", (role_name,))
            entries = []
            if role:
                rows = self.database.fetch_all(
                    """SELECT rt.provider_id,rt.model_id,rt.ordinal,p.name provider_name,
                              p.health_status,p.last_health_error,m.name model_name,m.status model_status,m.last_error,m.user_overrides_json
                       FROM routes r JOIN route_targets rt ON rt.route_id=r.id
                       JOIN providers p ON p.id=rt.provider_id JOIN models m ON m.id=rt.model_id
                       WHERE r.role_id=? AND r.enabled=1 ORDER BY r.priority,rt.ordinal""",
                    (role["id"],),
                )
                eligible = {(item.provider_id, item.model_id) for item in RoutingEngine(self.database).eligible_routes(role["id"])}
                entries = [{
                    "role": role_name, "provider": row["provider_name"], "model": row["model_name"],
                    "status": "eligible" if (row["provider_id"], row["model_id"]) in eligible else "unavailable",
                    "reason": row["last_error"] or (row["last_health_error"] if row["health_status"] not in {"healthy", "degraded"} else None),
                    "provider_health": row["health_status"], "model_status": row["model_status"],
                    "ordinal": row["ordinal"],
                    "residency": (json.loads(row["user_overrides_json"] or "{}").get("residency") if row["user_overrides_json"] else None),
                    "last_warmup": (json.loads(row["user_overrides_json"] or "{}").get("last_warmup") if row["user_overrides_json"] else None),
                } for row in rows]
            result["routes"][task_class] = entries
        codex = shutil.which(os.environ.get("VIRTIZAI_CODEX_BIN", "codex"))
        auth_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        result["workers"].append({
            "name": "Codex CLI worker", "type": "codex_worker",
            "available": bool(codex), "authenticated": (auth_home / "auth.json").exists(),
            "workspace_root": str(self.workspace_root),
        })
        result["routes"]["hard"] = [{"worker": "Codex CLI worker", "status": "available" if codex else "unavailable"}]
        result["routes"]["hard_fallback"] = [{"provider": "NeuralWatt", "status": "configured" if any(p["name"] == "NeuralWatt" for p in result["providers"]) else "not configured"}]
        return result

    def _metadata_row(self, row) -> dict:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        return {**metadata, "created_at": row["created_at"]}

    def _previous_assistant(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        row = self.database.fetch_one("SELECT provider_id, model_id, metadata_json, created_at FROM messages WHERE session_id=? AND role='assistant' ORDER BY rowid DESC LIMIT 1", (session_id,))
        return self._metadata_row(row) if row else None

    def _last_inference(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        row = self.database.fetch_one("SELECT provider_id, model_id, metadata_json, created_at FROM messages WHERE session_id=? AND role='assistant' ORDER BY rowid DESC LIMIT 50", (session_id,))
        if row is None:
            return None
        metadata = self._metadata_row(row)
        if metadata.get("execution_type") not in {"inference", "worker"}:
            return self._last_inference_for_older_message(session_id)
        return metadata

    def _last_inference_for_older_message(self, session_id: str) -> dict | None:
        for row in self.database.fetch_all("SELECT provider_id, model_id, metadata_json, created_at FROM messages WHERE session_id=? AND role='assistant' ORDER BY rowid DESC LIMIT 50", (session_id,)):
            metadata = self._metadata_row(row)
            if metadata.get("execution_type") in {"inference", "worker"}:
                return metadata
        return None


    def render(self, session_id: str | None = None, identity: bool = False) -> str:
        if identity == "warm_status":
            snapshot = self.snapshot()
            entries = snapshot["routes"].get("simple") or []
            if not entries:
                return "Secretary model is not configured."
            entry = entries[0]
            return f"Secretary model {entry.get('provider')}:{entry.get('model')} is {entry.get('residency') or 'not resident'} (route status: {entry.get('status')})."
        if identity:
            kind = identity if isinstance(identity, str) else "current"
            record = self._previous_assistant(session_id) if kind == "previous_response" else self._last_inference(session_id)
            if kind == "previous_response":
                if not record or record.get("execution_type") == "system":
                    return "That response was generated directly by VirtizAI introspection. No model was used. Tokens: 0."
            lines = ["This response is being generated directly by VirtizAI without an LLM call."]
            if not record:
                lines.append("There is no prior model or worker inference recorded in this session.")
                return "\n".join(lines)
            target = record.get("worker") or (f"{record.get('provider_name')}:{record.get('model_name')}" if record.get("provider_name") and record.get("model_name") else "unknown execution target")
            if kind == "current":
                lines = ["This response is being generated directly by VirtizAI without an LLM call.", f"The most recent actual inference in this session used: {target}."]
            else:
                prefix = "The most recent actual inference in this session used" if kind == "last_inference" else "That response used"
                lines = [f"{prefix}: {target}."]
            if record.get("fallback_used"):
                lines.append(f"It used the configured fallback because {record.get('fallback_reason') or 'the primary route was unavailable'}.")
            elif record.get("provider_name"):
                lines.append(f"Local/cloud classification: {record.get('locality') or 'unknown'}.")
            return "\n".join(lines)
        snapshot = self.snapshot()
        lines = ["Current VirtizAI routing configuration:"]
        labels = {"simple": "Secretary / Simple", "medium": "Medium", "hard": "Hard", "hard_fallback": "Hard fallback"}
        for key, label in labels.items():
            lines.append(label + ":")
            entries = snapshot["routes"].get(key) or [{"status": "not configured"}]
            for item in entries:
                target = item.get("worker") or (f"{item.get('provider')}:{item.get('model')}" if item.get("model") else item.get("provider", "route"))
                status = item.get("status", "unknown")
                reason = item.get("reason")
                lines.append(f"  {target} — {status}" + (f" ({reason})" if reason else ""))
        return "\n".join(lines)


class OperationalActionDetector:
    """Recognize unimplemented operational actions without pretending to execute them."""

    _verbs = {"restart", "stop", "start", "deploy", "update", "delete", "modify", "create", "run"}
    _targets = {"virtizai", "service", "gateway", "production", "prod", "dev", "development", "server", "container"}

    @classmethod
    def match(cls, content: str) -> str | None:
        tokens = re.findall(r"[a-z0-9]+", content.lower())
        if not tokens or tokens[0] not in cls._verbs or not set(tokens[1:]) & cls._targets:
            return None
        return tokens[0]

class SessionService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_user(self, user_id: str, display_name: str = "User") -> None:
        self.database.execute(
            """
            INSERT INTO users(id, display_name) VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name,
                                          updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, display_name),
        )

    def create_session(self, user_id: str, title: str | None = None) -> str:
        session_id = str(uuid.uuid4())
        self.database.execute(
            "INSERT INTO sessions(id, user_id, title) VALUES (?, ?, ?)",
            (session_id, user_id, title),
        )
        return session_id

    def get_affinity(self, session_id: str):
        return self.database.fetch_one(
            "SELECT affinity_provider_id, affinity_model_id, affinity_provider_name, affinity_model_name, affinity_reason, affinity_updated_at FROM sessions WHERE id=?",
            (session_id,),
        )

    def set_affinity(self, session_id: str, route, reason: str = "first_successful_inference") -> None:
        self.database.execute(
            """UPDATE sessions
               SET affinity_provider_id=?, affinity_model_id=?,
                   affinity_provider_name=?, affinity_model_name=?,
                   affinity_reason=?, affinity_updated_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (route.provider_id, route.model_id, route.provider_name, route.model_name, reason, session_id),
        )

    def clear_affinity(self, session_id: str, reason: str) -> None:
        self.database.execute(
            """UPDATE sessions SET affinity_provider_id=NULL, affinity_model_id=NULL,
               affinity_provider_name=NULL, affinity_model_name=NULL,
               affinity_reason=?, affinity_updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (reason, session_id),
        )

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> str:
        message_id = str(uuid.uuid4())
        details = metadata or {}
        self.database.execute(
            """
            INSERT INTO messages
                (id, session_id, role, content, provider_id, model_id, route_id,
                 input_tokens, output_tokens, total_tokens, latency_ms,
                 usage_exact, ttft_ms, estimated_cost, metadata_json)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                session_id,
                role,
                content,
                details.get("provider_id"),
                details.get("model_id"),
                details.get("route_id"),
                details.get("input_tokens"),
                details.get("output_tokens"),
                details.get("total_tokens"),
                details.get("latency_ms"),
                details.get("usage_exact"),
                details.get("ttft_ms"),
                details.get("estimated_cost"),
                json.dumps(details, sort_keys=True),
            ),
        )
        self.database.execute(
            "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        return message_id


class IntentPolicyEngine:
    def classify(self, content: str) -> str:
        normalized = content.strip().lower()
        if normalized.startswith(("run ", "deploy ", "change ", "build ", "fix ")):
            return "delegated"
        return "secretary"


class RouteResolver:
    def __init__(self, database: Database) -> None:
        self.database = database

    def resolve_secretary(self, strategy: str = "lowest_latency") -> list:
        role = self.database.fetch_one("SELECT id FROM roles WHERE id = 'role-secretary'")
        if role is None:
            return []
        return RoutingEngine(self.database).eligible_routes(role["id"], strategy)

    def resolve_role(self, role_name: str, strategy: str = "priority") -> list:
        role_id = RoutingEngine(self.database).role_id(role_name)
        return RoutingEngine(self.database).eligible_routes(role_id, strategy) if role_id else []


class CoreService:
    def __init__(
        self,
        database: Database,
        telemetry: TelemetryService,
        jobs: JobManager,
        providers: ProviderRegistry,
        codex_worker: CodexWorker | None = None,
        events=None,
    ) -> None:
        self.database = database
        self.telemetry = telemetry
        self.jobs = jobs
        self.providers = providers
        self.sessions = SessionService(database)
        self.policy = IntentPolicyEngine()
        self.classifier = TaskClassifier()
        self.introspection = IntrospectionService(database, Path(os.environ.get('VIRTIZAI_WORKSPACE_DIR', '/tmp/virtizai-workspace')))
        self.actions = OperationalActionDetector()
        self.codex_worker = codex_worker
        self.events = events
        self.routes = RouteResolver(database)

    async def _secretary_candidates(self) -> list:
        """Resolve the fast route and automatically re-probe cooled-down models."""
        route_candidates = self.routes.resolve_secretary()
        stale = self.database.fetch_all(
            """SELECT DISTINCT rt.provider_id FROM routes r
               JOIN route_targets rt ON rt.route_id=r.id
               JOIN models m ON m.id=rt.model_id
               WHERE r.role_id='role-secretary' AND r.enabled=1
                 AND m.status='error' AND m.updated_at <= datetime('now', '-30 seconds')"""
        )
        provider_ids = {row["provider_id"] for row in stale}
        if not route_candidates or provider_ids:
            for provider_id in provider_ids:
                try:
                    before = {
                        row["id"]: row["status"]
                        for row in self.database.fetch_all("SELECT id,status FROM models WHERE provider_id=?", (provider_id,))
                    }
                    await self.providers.discover_models(provider_id)
                    if self.events is not None:
                        for model_id, previous in before.items():
                            current = self.database.fetch_one("SELECT status FROM models WHERE id=?", (model_id,))
                            if previous == "error" and current and current["status"] in {"available", "warm", "cold", "loading"}:
                                row = self.database.fetch_one("SELECT p.name provider,m.name model FROM models m JOIN providers p ON p.id=m.provider_id WHERE m.id=?", (model_id,))
                                await self.events.transition("model", model_id, f"{row['provider']}:{row['model']}", "available", None, "info")
                except Exception:
                    continue
            route_candidates = self.routes.resolve_secretary()
        return route_candidates

    async def plan_operational_action(self, content: str) -> dict | None:
        """Interpret an action request into a closed, non-executing intent.

        The model only proposes one of two typed values.  Authorization,
        scope, confirmation, and execution remain in the gateway/tool layer;
        model prose can never perform or confirm a destructive operation.
        """
        candidates = await self._secretary_candidates()
        if not candidates:
            return None
        prompt = [
            {"role": "system", "content": (
                "Classify the user's request for VirtizAI's typed operational tools. "
                "Return JSON only, exactly one object with action and confidence. "
                "Allowed action values: discord_thread_cleanup, none. "
                "Use discord_thread_cleanup only when the user is asking to remove, "
                "prune, clean up, or delete Discord/server conversation threads. "
                "Questions, explanations, and ordinary conversation are none. "
                "Never claim that an action was executed. Example: "
                '{\"action\":\"discord_thread_cleanup\",\"confidence\":0.98}'
            )},
            {"role": "user", "content": content},
        ]
        try:
            inference = await asyncio.wait_for(
                self.providers.chat(candidates[0].provider_id, candidates[0].model_name, prompt, max_tokens=48),
                timeout=4.0,
            )
            match = re.search(r"\{\s*\"action\"\s*:\s*\"(discord_thread_cleanup|none)\".*?\"confidence\"\s*:\s*([0-9.]+).*?\}", inference.content, re.DOTALL)
            if match and match.group(1) == "discord_thread_cleanup" and float(match.group(2)) >= 0.75:
                return {"action": match.group(1), "confidence": float(match.group(2)), "model": candidates[0].model_name}
        except Exception:
            return None
        return None

    async def handle_message(
        self,
        user_id: str,
        session_id: str,
        content: str,
        display_name: str = "User",
        policy: CommunicationPolicy | None = None,
        interface_type: str | None = None,
        notification: dict | None = None,
    ) -> SecretaryResponse:
        request_id = str(uuid.uuid4())
        policy = policy or CommunicationPolicy()
        started = perf_counter()
        self.sessions.ensure_user(user_id, display_name)
        with self.telemetry.stage(request_id, "session_lookup"):
            session = self.database.fetch_one(
                "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            )
            if session is None:
                raise LookupError("Session not found")
        with self.telemetry.stage(request_id, "intent_policy"):
            classification = self.classifier.classify(content)
            intent = classification.kind
        action = self.actions.match(content)
        if action:
            self.sessions.add_message(session_id, "user", content)
            response_content = f"I did not perform that {action} action because no authorized typed tool is configured for this session."
            message_id = self.sessions.add_message(session_id, "assistant", response_content, {
                "execution_type": "system", "role": "secretary", "task_class": "simple",
                "action_requested": action, "action_executed": False,
                "tokens": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            })
            self.telemetry.record_event(request_id, "request_complete", json.dumps({
                "task_class": "simple", "execution_type": "system", "action_requested": action,
                "action_executed": False, "tokens": 0,
            }))
            return SecretaryResponse(request_id, session_id, message_id, response_content, None, None, None, latency_ms=(perf_counter() - started) * 1000, task_class="simple")
        if self.introspection.matches(content):
            self.sessions.add_message(session_id, "user", content)
            identity = self.introspection.identity_kind(content)
            response_content = self.introspection.render(session_id, identity=identity or False)
            message_id = self.sessions.add_message(session_id, "assistant", response_content, {
                "execution_type": "system", "role": "secretary", "task_class": "simple",
                "tokens": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            })
            self.telemetry.record_event(request_id, "request_complete", json.dumps({"task_class": "simple", "introspection": True, "execution_type": "system", "tokens": 0}))
            return SecretaryResponse(request_id, session_id, message_id, response_content, None, None, None, latency_ms=(perf_counter() - started) * 1000, task_class="simple")
        with self.telemetry.stage(request_id, "message_persist"):
            self.sessions.add_message(session_id, "user", content)
        route = None
        inference = None
        affinity = self.sessions.get_affinity(session_id)
        job_created = False
        job_id = None
        fallback_reason = None
        attempt_failures: list[dict[str, str]] = []
        recent_context = [
            {"role": row["role"], "content": row["content"]}
            for row in self.database.fetch_all(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 11",
                (session_id,),
            )
            ][::-1]
        if classification.kind == "simple":
            # Keep the interactive Secretary path concise so local inference
            # can complete within its measured latency budget.  This is a
            # routing/policy hint, not an owner-specific model instruction.
            recent_context.insert(0, {
                "role": "system",
                "content": "Answer as a fast secretary. Be concise: use at most three short bullet points and no long preamble.",
            })
        if classification.kind == "hard":
            worker_context = {
                "session_id": session_id,
                "interface_type": interface_type,
                "recent_messages": recent_context[-6:],
                "known_state": {
                    "classification": classification.kind,
                    "session_affinity": (
                        f"{affinity['affinity_provider_name']}:{affinity['affinity_model_name']}"
                        if affinity and affinity["affinity_provider_name"] else None
                    ),
                },
            }
            job_id = await self.jobs.submit(
                "codex_worker",
                {"prompt": content, "context": worker_context, "notification": notification or {}, "interface_type": interface_type},
                user_id=user_id,
                session_id=session_id,
            )
            response_content = f"Codex worker job accepted: {job_id}."
            job_created = True
        else:
            role_name = "secretary" if classification.kind == "simple" else "general-reasoning"
            route_stage = "secretary_route" if classification.kind == "simple" else "medium_route"
            with self.telemetry.stage(request_id, route_stage):
                if affinity and affinity["affinity_provider_id"] and classification.kind != "hard":
                    # A session's conversational model is stable; task class may
                    # select tools/workers, but must not silently switch the voice.
                    candidates = [
                        item for item in await self._secretary_candidates()
                        if item.provider_id == affinity["affinity_provider_id"]
                        and item.model_id == affinity["affinity_model_id"]
                    ]
                    if not candidates:
                        candidates = await self._secretary_candidates()
                else:
                    candidates = self.routes.resolve_role(role_name) if classification.kind == "medium" else await self._secretary_candidates()
            total_budget = float(os.environ.get(
                "VIRTIZAI_SECRETARY_TIMEOUT_SECONDS" if classification.kind == "simple" else "VIRTIZAI_MEDIUM_TIMEOUT_SECONDS",
                "15" if classification.kind == "simple" else "120",
            ))
            attempt_budget = float(os.environ.get(
                "VIRTIZAI_SECRETARY_ATTEMPT_TIMEOUT_SECONDS" if classification.kind == "simple" else "VIRTIZAI_MEDIUM_ATTEMPT_TIMEOUT_SECONDS",
                "10" if classification.kind == "simple" else "120",
            ))
            # Keep interactive Secretary generations bounded.  The normal
            # communication policy allows up to 4096 tokens, which is useful
            # for general reasoning but can make a short local request exceed
            # the Secretary attempt budget before fallback is possible.
            secretary_max_tokens = int(os.environ.get("VIRTIZAI_SECRETARY_MAX_TOKENS", "32"))
            max_tokens = policy.output_token_budget(
                default=secretary_max_tokens if classification.kind == "simple" else 8192
            )
            deadline = perf_counter() + total_budget
            for candidate in candidates:
                remaining = deadline - perf_counter()
                if remaining <= 0:
                    break
                candidate_timeout = min(attempt_budget, remaining)
                try:
                    with self.telemetry.stage(request_id, "provider_inference"):
                        inference = await asyncio.wait_for(
                            # The current user message is already in recent_context after
                            # persistence; appending it again doubled prompts and caused
                            # avoidable Secretary timeouts.
                            self.providers.chat(candidate.provider_id, candidate.model_name, recent_context, max_tokens=max_tokens),
                            timeout=candidate_timeout,
                        )
                    route = candidate
                    if not affinity or not affinity["affinity_provider_id"]:
                        self.sessions.set_affinity(session_id, candidate, "first_successful_inference")
                    elif candidate.provider_id != affinity["affinity_provider_id"] or candidate.model_id != affinity["affinity_model_id"]:
                        self.sessions.set_affinity(session_id, candidate, "affinity_model_unavailable_fallback")
                    if self.events is not None:
                        await self.events.transition("model", candidate.model_id, f"{candidate.provider_name}:{candidate.model_name}", "available", None, "info")
                    self.database.execute(
                        "UPDATE models SET status='available', last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (candidate.model_id,),
                    )
                    break
                except Exception as exc:
                    reason = str(exc).strip() or f"timed out after {candidate_timeout:.1f}s"
                    if fallback_reason is None:
                        fallback_reason = reason
                    attempt_failures.append({"provider": candidate.provider_name, "model": candidate.model_name, "reason": reason})
                    if self.events is not None:
                        await self.events.transition("model", candidate.model_id, f"{candidate.provider_name}:{candidate.model_name}", "degraded", reason, "warning")
                    self.database.execute(
                        "UPDATE models SET status='error', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (reason, candidate.model_id),
                    )
                    continue
            if route is None and attempt_failures:
                label = "Secretary" if classification.kind == "simple" else classification.kind.title()
                response_content = f"{label} routes failed:\n" + "\n".join(
                    f"- {item['provider']}:{item['model']}: {item['reason']}" for item in attempt_failures
                )
            else:
                response_content = f"{classification.kind.title()} route unavailable; configure an eligible provider/model route." if route is None else inference.content
        latency_ms = (perf_counter() - started) * 1000
        message_id = self.sessions.add_message(
            session_id,
            "assistant",
            response_content,
            {
                "execution_type": "worker" if job_created else ("inference" if route else "system"),
                "role": "codex_worker" if job_created else "secretary",
                "task_class": classification.kind,
                "provider_id": route.provider_id if route else None,
                "provider_name": route.provider_name if route else None,
                "model_id": route.model_id if route else None,
                "model_name": route.model_name if route else None,
                "route_id": route.route_id if route else None,
                "worker": "Codex CLI worker" if job_created else None,
                "job_id": job_id,
                "locality": "local" if route and route.provider_name and "NeuralWatt" not in route.provider_name else ("cloud" if route else None),
                "fallback_used": bool(route and getattr(route, "ordinal", 0) > 0),
                "fallback_reason": fallback_reason,
                "session_affinity": bool(affinity and affinity["affinity_provider_id"]),
                "attempt_failures": attempt_failures,
                "latency_ms": latency_ms,
                "ttft_ms": inference.ttft_ms if inference else None,
                "input_tokens": inference.input_tokens if inference else 0,
                "output_tokens": inference.output_tokens if inference else 0,
                "total_tokens": inference.total_tokens if inference else 0,
                "usage_exact": inference.usage_exact if inference else None,
                "estimated_cost": inference.estimated_cost if inference else None,
            },
        )
        self.telemetry.record_event(request_id, "request_complete", json.dumps({"intent": intent, "task_class": classification.kind, "classification_reason": classification.reason, "worker": "codex" if classification.kind == "hard" else None}))
        return SecretaryResponse(
            request_id=request_id,
            session_id=session_id,
            message_id=message_id,
            content=response_content,
            route_id=route.route_id if route else None,
            provider_id=route.provider_id if route else None,
            model_id=route.model_id if route else None,
            provider_name=route.provider_name if route else None,
            model_name=route.model_name if route else None,
            latency_ms=latency_ms,
            ttft_ms=inference.ttft_ms if inference else None,
            input_tokens=inference.input_tokens if inference else None,
            output_tokens=inference.output_tokens if inference else None,
            total_tokens=inference.total_tokens if inference else None,
            usage_exact=inference.usage_exact if inference else None,
            estimated_cost=inference.estimated_cost if inference else None,
            job_created=job_created,
            task_class=classification.kind,
        )
