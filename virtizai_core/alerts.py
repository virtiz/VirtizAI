from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from .db import Database
Listener = Callable[[dict], Awaitable[None]]

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class OperationalEventService:
    """Persist and fan out meaningful state transitions, with deduplication."""
    def __init__(self, database: Database) -> None:
        self.database = database
        self.listeners: list[Listener] = []
    def subscribe(self, listener: Listener) -> None:
        self.listeners.append(listener)
    async def transition(self, component_type: str, component_id: str, component_name: str, new_state: str, reason: str | None = None, severity: str = 'info', initial: bool = False) -> dict | None:
        previous = self.database.fetch_one("SELECT new_state FROM operational_events WHERE component_type=? AND component_id=? ORDER BY created_at DESC LIMIT 1", (component_type, component_id))
        previous_state = previous['new_state'] if previous else None
        if previous_state == new_state:
            return None
        event = {'id': str(uuid.uuid4()), 'component_type': component_type, 'component_id': component_id, 'component_name': component_name, 'previous_state': previous_state, 'new_state': new_state, 'reason': reason, 'severity': severity, 'initial_state': bool(initial or previous is None), 'notification_status': 'suppressed' if (initial or previous is None) else 'pending', 'created_at': now_iso()}
        self.database.execute("INSERT INTO operational_events (id,component_type,component_id,component_name,previous_state,new_state,reason,severity,initial_state,notification_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(event.values()))
        if event['notification_status'] == 'pending':
            for listener in tuple(self.listeners):
                try: await listener(event)
                except Exception: continue
        return event
    def history(self, limit: int = 100) -> list[dict]:
        return [dict(row) for row in self.database.fetch_all("SELECT * FROM operational_events ORDER BY created_at DESC LIMIT ?", (min(limit, 500),))]
