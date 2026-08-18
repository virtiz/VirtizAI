from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from time import perf_counter

from .db import Database
from .jobs import JobManager
from .providers import ProviderRegistry
from .routing import RoutingEngine
from .telemetry import TelemetryService
from .policy import CommunicationPolicy


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


class CoreService:
    def __init__(
        self,
        database: Database,
        telemetry: TelemetryService,
        jobs: JobManager,
        providers: ProviderRegistry,
    ) -> None:
        self.database = database
        self.telemetry = telemetry
        self.jobs = jobs
        self.providers = providers
        self.sessions = SessionService(database)
        self.policy = IntentPolicyEngine()
        self.routes = RouteResolver(database)

    async def handle_message(
        self,
        user_id: str,
        session_id: str,
        content: str,
        display_name: str = "User",
        policy: CommunicationPolicy | None = None,
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
            intent = self.policy.classify(content)
        with self.telemetry.stage(request_id, "message_persist"):
            self.sessions.add_message(session_id, "user", content)
        if intent == "delegated":
            job_id = await self.jobs.submit(
                "delegated_request",
                {"content": content},
                user_id=user_id,
                session_id=session_id,
            )
            response_content = f"Delegated work accepted as job {job_id}."
            route = None
            job_created = True
        else:
            with self.telemetry.stage(request_id, "secretary_route"):
                candidates = self.routes.resolve_secretary()
            route = None
            inference = None
            for candidate in candidates:
                try:
                    with self.telemetry.stage(request_id, "provider_inference"):
                        inference = await self.providers.chat(candidate.provider_id, candidate.model_name, [{"role": "user", "content": content}], max_tokens=policy.output_token_budget())
                    route = candidate
                    break
                except Exception:
                    continue
            response_content = (
                "Secretary route is ready; configure an eligible provider/model route."
                if route is None
                else inference.content
            )
            job_created = False
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
        self.telemetry.record_event(request_id, "request_complete", json.dumps({"intent": intent}))
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
        )
