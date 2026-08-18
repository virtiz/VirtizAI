from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .migrations import apply_migrations


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def open(self) -> None:
        if self.connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        apply_migrations(self.connection)

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        if self.connection is None:
            raise RuntimeError("Database is not open")
        return self.connection.execute(sql, parameters)

    def fetch_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.execute(sql, parameters).fetchone()

    def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.execute(sql, parameters).fetchall()

    def transaction(self):
        if self.connection is None:
            raise RuntimeError("Database is not open")
        return self.connection
