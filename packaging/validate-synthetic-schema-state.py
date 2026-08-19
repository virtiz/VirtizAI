#!/usr/bin/env python3
"""Seed, capture, and verify the Phase 10 synthetic schema transition state."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path

def snapshot(connection: sqlite3.Connection) -> dict:
    value = connection.execute("SELECT value FROM app_meta WHERE key='synthetic_transition'").fetchone()
    proof = connection.execute("SELECT transformed_value FROM schema_transition_proof WHERE id='synthetic-transition'").fetchone()
    schema = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    return {"schema_version": schema, "synthetic_transition": value[0] if value else None, "schema_transition_proof": proof[0] if proof else None}

parser = argparse.ArgumentParser()
parser.add_argument("--database", type=Path, default=Path("/var/lib/virtizai/virtizai.db"))
parser.add_argument("--mode", choices=("seed", "capture", "assert-baseline", "assert-migrated"), required=True)
parser.add_argument("--expected", type=Path)
args = parser.parse_args()
database = sqlite3.connect(args.database)
if args.mode == "seed":
    database.execute("INSERT INTO app_meta(key, value) VALUES ('synthetic_transition', 'schema-10-baseline') ON CONFLICT(key) DO UPDATE SET value='schema-10-baseline'")
    database.execute("DROP TABLE IF EXISTS schema_transition_proof")
    database.commit()
state = snapshot(database)
if args.mode == "capture":
    if args.expected is None:
        parser.error("--expected is required for capture")
    args.expected.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
elif args.mode == "assert-baseline":
    assert state["schema_version"] == 10, state
    assert state["synthetic_transition"] == "schema-10-baseline", state
    assert state["schema_transition_proof"] is None, state
elif args.mode == "assert-migrated":
    assert state["schema_version"] == 11, state
    assert state["synthetic_transition"] == "schema-11-transformed", state
    assert state["schema_transition_proof"] == "schema-11-only", state
print(json.dumps(state, sort_keys=True))
