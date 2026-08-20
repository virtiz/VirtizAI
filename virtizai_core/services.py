from __future__ import annotations

import json
import uuid
import os
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
        "provider configuration", "configured providers", "why are the local models",
        "which models are healthy", "why did you fall back", "why can't you use",
        "which providers are down", "status of the medium route", "why are models unavailable",
    )

    def __init__(self, database: Database, workspace_root: Path) -> None:
        self.database = database
        self.workspace_root = workspace_root

    def matches(self, content: str) -> bool:
        text = content.strip().lower()
        return any(signal in text for signal in self._signals)

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
                              p.health_status,p.last_health_error,m.name model_name,m.status model_status,m.last_error
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

    def render(self) -> str:
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
                 usage_exact, ttft_ms, estimated_cost)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        role = self.database.fetch_one("SELECT id FROM roles WHERE name = 'secretary'")
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
        self.codex_worker = codex_worker
        self.events = events
        self.routes = RouteResolver(database)

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
                "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            )
            if session is None:
                raise LookupError("Session not found")
        with self.telemetry.stage(request_id, "intent_policy"):
            classification = self.classifier.classify(content)
            intent = classification.kind
        if self.introspection.matches(content):
            self.sessions.add_message(session_id, "user", content)
            response_content = self.introspection.render()
            message_id = self.sessions.add_message(session_id, "assistant", response_content)
            self.telemetry.record_event(request_id, "request_complete", json.dumps({"task_class": "simple", "introspection": True, "tokens": 0}))
            return SecretaryResponse(request_id, session_id, message_id, response_content, None, None, None, latency_ms=(perf_counter() - started) * 1000, task_class="simple")
        with self.telemetry.stage(request_id, "message_persist"):
            self.sessions.add_message(session_id, "user", content)
        route = None
        inference = None
        job_created = False
        recent_context = [
            {"role": row["role"], "content": row["content"]}
            for row in self.database.fetch_all(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 11",
                (session_id,),
            )
        ][::-1]
        if classification.kind == "hard":
            job_id = await self.jobs.submit(
                "codex_worker",
                {"prompt": content, "notification": notification or {}, "interface_type": interface_type},
                user_id=user_id,
                session_id=session_id,
            )
            response_content = f"Codex worker job accepted: {job_id}."
            job_created = True
        else:
            role_name = "secretary" if classification.kind == "simple" else "general-reasoning"
            route_stage = "secretary_route" if classification.kind == "simple" else "medium_route"
            with self.telemetry.stage(request_id, route_stage):
                candidates = self.routes.resolve_role(role_name) if classification.kind == "medium" else self.routes.resolve_secretary()
            for candidate in candidates:
                try:
                    with self.telemetry.stage(request_id, "provider_inference"):
                        inference = await self.providers.chat(candidate.provider_id, candidate.model_name, recent_context + [{"role": "user", "content": content}], max_tokens=policy.output_token_budget())
                    route = candidate
                    if self.events is not None:
                        await self.events.transition("model", candidate.model_id, f"{candidate.provider_name}:{candidate.model_name}", "available", None, "info")
                    self.database.execute(
                        "UPDATE models SET status='available', last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (candidate.model_id,),
                    )
                    break
                except Exception as exc:
                    if self.events is not None:
                        await self.events.transition("model", candidate.model_id, f"{candidate.provider_name}:{candidate.model_name}", "unavailable", str(exc)[:300], "error")
                    self.database.execute(
                        "UPDATE models SET status='error', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (str(exc)[:300], candidate.model_id),
                    )
                    continue
            response_content = (
                f"{classification.kind.title()} route unavailable; configure an eligible provider/model route."
                if route is None
                else inference.content
            )
        latency_ms = (perf_counter() - started) * 1000
        message_id = self.sessions.add_message(
            session_id,
            "assistant",
            response_content,
            {
                "provider_id": route.provider_id if route else None,
                "model_id": route.model_id if route else None,
                "route_id": route.route_id if route else None,
                "latency_ms": latency_ms,
                "ttft_ms": inference.ttft_ms if inference else None,
                "input_tokens": inference.input_tokens if inference else None,
                "output_tokens": inference.output_tokens if inference else None,
                "total_tokens": inference.total_tokens if inference else None,
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
