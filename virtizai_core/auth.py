from __future__ import annotations

from .db import Database


class AuthAdminService:
    """Core boundary for future authentication and administration adapters."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def user_exists(self, user_id: str) -> bool:
        return self.database.fetch_one("SELECT 1 FROM users WHERE id = ?", (user_id,)) is not None

    def application_version(self) -> str:
        row = self.database.fetch_one("SELECT value FROM app_meta WHERE key = 'application_name'")
        return str(row["value"]) if row else "VirtizAI"
