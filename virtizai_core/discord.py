from __future__ import annotations

from dataclasses import dataclass
import json
import uuid

from .interfaces import InterfaceRequest, InterfaceService
from .registries import UpdateManager


@dataclass(frozen=True)
class DiscordReply:
    content: str
    session_id: str
    metadata: dict


class DiscordAdapter:
    """Shared Discord interface adapter used by the Gateway transport and HTTP API."""

    def __init__(self, interfaces: InterfaceService, updates: UpdateManager) -> None:
        self.interfaces = interfaces
        self.updates = updates
        self.pending_confirmations: dict[str, tuple[str, str]] = {}
        self.release_events: list[dict] = []

    async def handle_message(self, user_id: str, content: str, session_key: str | None = None, session_id: str | None = None, display_name: str = "Discord user") -> DiscordReply:
        session_id, response = await self.interfaces.handle(InterfaceRequest("discord", user_id, content, session_key=session_key, session_id=session_id, display_name=display_name))
        metadata = {"model": response.model_name or response.model_id, "provider": response.provider_name or response.provider_id, "tokens": response.total_tokens, "latency_ms": response.latency_ms, "local": bool(response.provider_id), "job_created": response.job_created, "task_class": response.task_class}
        return DiscordReply(response.content, session_id, metadata)

    def authorized(self, user_id: str, command: str) -> bool:
        row = self.interfaces.database.fetch_one("SELECT admin_users_json FROM discord_config WHERE id = 'discord-default'")
        if command in {"status", "models", "providers", "jobs", "releases"}:
            return True
        import json
        return user_id in json.loads(row["admin_users_json"] if row else "[]")

    async def command(self, user_id: str, command: str) -> dict:
        if not self.authorized(user_id, command):
            return {"error": "permission_denied"}
        if command == "status":
            return {"status": "ok"}
        if command == "models":
            return {"models": [dict(row) for row in self.interfaces.database.fetch_all("SELECT * FROM models") ]}
        if command == "providers":
            return {"providers": [dict(row) for row in self.interfaces.database.fetch_all("SELECT id,name,adapter_type,health_status FROM providers") ]}
        if command == "jobs":
            return {"jobs": [dict(row) for row in self.interfaces.database.fetch_all("SELECT id,status,kind,created_at FROM jobs ORDER BY created_at DESC LIMIT 20") ]}
        if command == "releases":
            return {
                "releases": self.updates.releases(),
                "policy": self.updates.policy(),
                "history": self.updates.history(),
            }
        if command in {"update", "rollback"}:
            confirmation = str(uuid.uuid4())
            self.pending_confirmations[confirmation] = (user_id, command)
            return {"status": "confirmation_required", "confirmation_id": confirmation, "action": command}
        return {"error": "unknown_command"}

    def confirm_update(self, user_id: str, confirmation_id: str) -> dict:
        pending = self.pending_confirmations.pop(confirmation_id, None)
        if pending is None or pending[0] != user_id:
            return {"error": "confirmation_denied"}
        return {"status": "confirmed", "action": pending[1]}

    def emit_release_event(self, event_type: str, payload: dict) -> dict:
        allowed = {"release_available", "update_completed", "update_failed", "rollback_completed", "external_update_detected"}
        if event_type not in allowed:
            raise ValueError("Unsupported release event")
        row = self.interfaces.database.fetch_one("SELECT release_channel_id FROM discord_config WHERE id='discord-default'")
        event = {"event_type": event_type, "channel_id": row["release_channel_id"] if row else None, "payload": payload}
        self.release_events.append(event)
        self.interfaces.database.execute("INSERT INTO interface_events(interface_type, event_type, metadata_json) VALUES ('discord', ?, ?)", (event_type, json.dumps(event)))
        return event
