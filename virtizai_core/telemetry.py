from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .db import Database


@dataclass(frozen=True)
class StageTiming:
    request_id: str
    stage: str
    duration_ms: float


class TelemetryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @contextmanager
    def stage(self, request_id: str, stage: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            self.database.execute(
                """
                INSERT INTO telemetry_events
                    (request_id, event_type, stage, duration_ms, metadata_json)
                VALUES (?, 'request_stage', ?, ?, '{}')
                """,
                (request_id, stage, duration_ms),
            )

    def record_event(
        self,
        request_id: str | None,
        event_type: str,
        metadata_json: str = "{}",
    ) -> None:
        self.database.execute(
            """
            INSERT INTO telemetry_events
                (request_id, event_type, stage, duration_ms, metadata_json)
            VALUES (?, ?, NULL, NULL, ?)
            """,
            (request_id, event_type, metadata_json),
        )
