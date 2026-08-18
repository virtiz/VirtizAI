from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from .db import Database

JobHandler = Callable[[str, dict], Awaitable[dict]]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.handlers: dict[str, JobHandler] = {}

    def register_handler(self, kind: str, handler: JobHandler) -> None:
        self.handlers[kind] = handler

    async def submit(
        self,
        kind: str,
        payload: dict,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        job_id = str(uuid.uuid4())
        self.database.execute(
            """
            INSERT INTO jobs(id, user_id, session_id, kind, status, payload_json)
            VALUES (?, ?, ?, ?, 'queued', ?)
            """,
            (job_id, user_id, session_id, kind, json.dumps(payload)),
        )
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, kind, payload))
        return job_id

    async def _run(self, job_id: str, kind: str, payload: dict) -> None:
        started = now_iso()
        self.database.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
            (started, job_id),
        )
        try:
            handler = self.handlers.get(kind)
            if handler is None:
                raise LookupError(f"No handler registered for job kind: {kind}")
            result = await handler(job_id, payload)
            self.database.execute(
                "UPDATE jobs SET status = 'succeeded', result_json = ?, finished_at = ? WHERE id = ?",
                (json.dumps(result), now_iso(), job_id),
            )
        except Exception as exc:
            self.database.execute(
                "UPDATE jobs SET status = 'failed', result_json = ?, finished_at = ? WHERE id = ?",
                (json.dumps({"error": type(exc).__name__}), now_iso(), job_id),
            )
        finally:
            self.tasks.pop(job_id, None)

    def get(self, job_id: str) -> dict | None:
        row = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return dict(row) if row else None

    async def wait_for_idle(self) -> None:
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()))
