from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from .db import Database
from .services import CoreService, SecretaryResponse
from .policy import CommunicationPolicy, normalize_policy


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

    def __init__(self, database: Database, core: CoreService) -> None:
        self.database = database
        self.core = core

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
        response = await self.core.handle_message(
            user_id, session_id, request.content, request.display_name, policy,
            request.interface_type,
            {"interface": request.interface_type, "external_subject": request.external_subject},
        )
        self.database.execute("INSERT INTO interface_events(interface_type, user_id, session_id, event_type, metadata_json) VALUES (?, ?, ?, 'message', ?)", (request.interface_type, user_id, session_id, json.dumps({"request_id": response.request_id})))
        return session_id, response

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
