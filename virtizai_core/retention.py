from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import Database


class RetentionService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def prune(self, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        counts = {}
        for event_type in ("request_stage", "context_build", "execution_audit"):
            row = self.database.fetch_one("SELECT retention_days FROM telemetry_retention WHERE event_type = ?", (event_type,))
            if not row:
                continue
            cutoff = (now - timedelta(days=row["retention_days"])).strftime("%Y-%m-%d %H:%M:%S")
            table = "telemetry_events" if event_type in {"request_stage", "context_build"} else "execution_audit"
            if table == "telemetry_events":
                cursor = self.database.execute("DELETE FROM telemetry_events WHERE event_type = ? AND created_at < ?", ("request_stage" if event_type == "request_stage" else "context_build", cutoff))
            else:
                cursor = self.database.execute("DELETE FROM execution_audit WHERE created_at < ?", (cutoff,))
            counts[event_type] = cursor.rowcount
        return counts

    def configure(self, event_type: str, retention_days: int) -> None:
        self.database.execute("UPDATE telemetry_retention SET retention_days = ? WHERE event_type = ?", (retention_days, event_type))
