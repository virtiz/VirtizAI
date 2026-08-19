#!/usr/bin/env python3
"""Structured recovery and external-update validation.

The harness is target-agnostic: pass the guest API URL and database path (or
only the API URL for remote guests). It records the API result, health, latest
history, backup, and external transaction-journal state as one JSON document.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def call(url: str, payload: dict) -> tuple[int, dict]:
    request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        body = error.read().decode()
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"detail": body}


def health(base_url: str) -> dict:
    with urlopen(base_url.rstrip("/") + "/healthz", timeout=10) as response:
        return json.loads(response.read())


def state(database: Path | None, journal: Path | None) -> dict:
    result: dict = {"history": [], "backups": [], "transactions": []}
    if database and database.exists():
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        result["history"] = [dict(row) for row in connection.execute("SELECT id, version, action, status, metadata_json FROM update_history ORDER BY rowid DESC LIMIT 10")]
        result["backups"] = [dict(row) for row in connection.execute("SELECT backup_ref, checksum_sha256, verified FROM update_backups ORDER BY rowid DESC LIMIT 10")]
        connection.close()
    if journal and journal.exists():
        result["transactions"] = [json.loads(path.read_text()) for path in sorted(journal.glob("*.json"))]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--source", choices=("native_package", "docker_compose"))
    parser.add_argument("--old-version")
    parser.add_argument("--new-version")
    parser.add_argument("--health", default="healthy")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--target-version")
    parser.add_argument("--target-schema", type=int)
    parser.add_argument("--expected-status", type=int)
    parser.add_argument("--expected-code")
    parser.add_argument("--wait-seconds", type=int, default=3)
    args = parser.parse_args()
    evidence: dict = {"requested_at": time.time()}
    if args.source:
        if not args.old_version or not args.new_version:
            parser.error("--old-version and --new-version are required for external detection")
        status, response = call(args.base_url.rstrip("/") + "/v1/updates/external", {"old_version": args.old_version, "new_version": args.new_version, "source": args.source, "health": args.health})
        evidence.update({"operation": "external_update", "http_status": status, "api": response})
    else:
        if not args.artifact or not args.target_version:
            parser.error("--artifact and --target-version are required for native failure validation")
        payload = {"artifact_path": str(args.artifact), "sha256": hashlib.sha256(args.artifact.read_bytes()).hexdigest(), "target_version": args.target_version}
        if args.target_schema is not None:
            payload["target_schema"] = args.target_schema
        status, response = call(args.base_url.rstrip("/") + "/v1/updates/native/apply", payload)
        evidence.update({"operation": "native_failure", "http_status": status, "api": response})
    time.sleep(args.wait_seconds)
    try:
        evidence["health"] = health(args.base_url)
    except (OSError, URLError) as error:
        evidence["health_error"] = str(error)
    evidence["state"] = state(args.database, args.journal)
    if args.expected_status is not None and evidence["http_status"] != args.expected_status:
        raise SystemExit(json.dumps({"error": "unexpected_http_status", "evidence": evidence}, sort_keys=True))
    if args.expected_code and evidence.get("api", {}).get("detail", {}).get("code") != args.expected_code:
        raise SystemExit(json.dumps({"error": "unexpected_failure_code", "evidence": evidence}, sort_keys=True))
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
