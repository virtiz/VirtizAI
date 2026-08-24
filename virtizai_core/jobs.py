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
        self.listeners: list[Callable[[dict], Awaitable[None]]] = []

    def register_handler(self, kind: str, handler: JobHandler) -> None:
        self.handlers[kind] = handler

    def register_listener(self, listener: Callable[[dict], Awaitable[None]]) -> None:
        self.listeners.append(listener)

    async def _notify(self, job_id: str) -> None:
        event = self.get(job_id)
        if not event:
            return
        for listener in tuple(self.listeners):
            try:
                await listener(event)
            except Exception:
                continue

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
            final_status = "succeeded" if result.get("status") in {None, "succeeded"} else "failed"
            self.database.execute(
                "UPDATE jobs SET status = ?, result_json = ?, finished_at = ? WHERE id = ?",
                (final_status, json.dumps(result), now_iso(), job_id),
            )
            await self._notify(job_id)
        except asyncio.CancelledError:
            self.database.execute(
                "UPDATE jobs SET status = 'cancelled', result_json = ?, finished_at = ? WHERE id = ?",
                (json.dumps({"worker": kind, "status": "cancelled"}), now_iso(), job_id),
            )
            await self._notify(job_id)
            raise
        except Exception as exc:
            self.database.execute(
                "UPDATE jobs SET status = 'failed', result_json = ?, finished_at = ? WHERE id = ?",
                (json.dumps({"error": type(exc).__name__}), now_iso(), job_id),
            )
            await self._notify(job_id)
        finally:
            self.tasks.pop(job_id, None)

    def create_delegated(
        self,
        *,
        kind: str,
        payload: dict,
        user_id: str | None,
        session_id: str | None,
        project_id: str | None,
        role_id: str | None,
        provider_id: str | None,
        model_id: str | None,
        worker_id: str | None,
        environment_target_id: str | None,
        objective: str | None,
    ) -> str:
        """Persist a delegated job without scheduling execution."""
        job_id = str(uuid.uuid4())
        self.database.execute(
            """
            INSERT INTO jobs(
                id, user_id, session_id, project_id, role_id, provider_id, model_id,
                worker_id, environment_target_id, kind, status, objective, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                job_id, user_id, session_id, project_id, role_id, provider_id, model_id,
                worker_id, environment_target_id, kind, objective, json.dumps(payload),
            ),
        )
        return job_id

    def transition(self, job_id: str, status: str) -> dict | None:
        """Apply the bounded state machine for a durable delegated job."""
        job = self.get(job_id)
        if job is None:
            return None
        allowed = {
            "queued": {"running", "cancelled"},
            "running": {"succeeded", "failed", "cancelled"},
        }
        if status not in allowed.get(job["status"], set()):
            raise ValueError(f"Job cannot transition from {job['status']} to {status}.")
        timestamp_column = "started_at" if status == "running" else "finished_at"
        self.database.execute(
            f"UPDATE jobs SET status = ?, {timestamp_column} = ? WHERE id = ?",
            (status, now_iso(), job_id),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        row = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return dict(row) if row else None

    def cancel(self, job_id: str) -> bool:
        task = self.tasks.get(job_id)
        if task is None:
            return False
        task.cancel()
        return True

    async def wait_for_idle(self) -> None:
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
