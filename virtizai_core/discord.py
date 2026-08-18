from __future__ import annotations

from dataclasses import dataclass

from .interfaces import InterfaceRequest, InterfaceService


@dataclass(frozen=True)
class DiscordReply:
    content: str
    session_id: str
    metadata: dict


class DiscordAdapter:
    """In-process adapter; transport integration is optional and injectable."""

    def __init__(self, interfaces: InterfaceService) -> None:
        self.interfaces = interfaces

    async def handle_message(self, user_id: str, content: str, session_key: str | None = None, session_id: str | None = None, display_name: str = "Discord user") -> DiscordReply:
        session_id, response = await self.interfaces.handle(InterfaceRequest("discord", user_id, content, session_key=session_key, session_id=session_id, display_name=display_name))
        metadata = {"model": response.model_name or response.model_id, "provider": response.provider_name or response.provider_id, "tokens": response.total_tokens, "latency_ms": response.latency_ms, "local": bool(response.provider_id)}
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
            return {"releases": []}
        return {"error": "unknown_command"}
