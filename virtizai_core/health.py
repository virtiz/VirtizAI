from __future__ import annotations

from datetime import datetime, timezone

from .adapters import ProviderAdapter, ProviderHealth
from .db import Database


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HealthManager:
    """Hysteretic provider health: transient failures do not immediately flap routes."""

    def __init__(self, database: Database, adapters: dict[str, ProviderAdapter]) -> None:
        self.database = database
        self.adapters = adapters

    async def check_provider(self, provider_id: str) -> ProviderHealth:
        row = self.database.fetch_one(
            "SELECT * FROM providers WHERE id = ?", (provider_id,)
        )
        if row is None:
            raise LookupError(f"Unknown provider: {provider_id}")
        if not row["enabled"]:
            self.database.execute(
                "UPDATE providers SET health_status = 'disabled', last_health_check_at = ? WHERE id = ?",
                (now_iso(), provider_id),
            )
            return ProviderHealth("disabled")
        adapter = self.adapters.get(provider_id)
        if adapter is None:
            return ProviderHealth("unknown", error="adapter not registered")
        result = await adapter.health()
        current = row["health_status"]
        failures = row["failure_count"]
        successes = row["success_count"]
        if result.state == "healthy":
            successes += 1
            failures = 0
            next_state = "healthy" if successes >= row["recovery_threshold"] or current in {"healthy", "unknown"} else current
        else:
            failures += 1
            successes = 0
            next_state = "unavailable" if failures >= row["failure_threshold"] else ("degraded" if current == "healthy" else current)
        self.database.execute(
            """
            UPDATE providers
            SET health_status = ?, failure_count = ?, success_count = ?,
                last_health_check_at = ?, last_health_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (next_state, failures, successes, now_iso(), result.error, provider_id),
        )
        return ProviderHealth(next_state, result.latency_ms, result.error, result.capabilities)

    def set_provider_status(self, provider_id: str, status: str) -> None:
        self.database.execute(
            "UPDATE providers SET health_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, provider_id),
        )
