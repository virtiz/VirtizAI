#!/usr/bin/env python3
"""Print local package, health, update history, and backup state for release validation."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from urllib.request import urlopen

package = subprocess.check_output(["dpkg-query", "-W", "virtizai"], text=True).strip()
with urlopen("http://127.0.0.1:8766/healthz", timeout=5) as response:
    health = json.load(response)
database = sqlite3.connect("/var/lib/virtizai/virtizai.db")
history = database.execute("SELECT version, action, status, metadata_json FROM update_history ORDER BY created_at DESC LIMIT 5").fetchall()
backups = database.execute("SELECT backup_ref, checksum_sha256, verified FROM update_backups ORDER BY created_at DESC LIMIT 5").fetchall()
print(json.dumps({"package": package, "health": health, "history": history, "backups": backups}, indent=2))
