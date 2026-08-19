#!/usr/bin/env python3
"""Call the local native Update Manager API with typed JSON and print its response."""
from __future__ import annotations

import argparse
import json
import os
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default=os.environ.get("VIRTIZAI_BASE_URL", "http://127.0.0.1:8766"))
parser.add_argument("--artifact", required=True)
parser.add_argument("--sha256", required=True)
parser.add_argument("--target-version", required=True)
parser.add_argument("--wait-health", action="store_true")
parser.add_argument("--operation", choices=("apply", "rollback"), default="apply")
parser.add_argument("--target-schema", type=int)
parser.add_argument("--restore-data", action="store_true")
parser.add_argument("--backup-ref")
parser.add_argument("--backup-sha256")
args = parser.parse_args()
payload = {"artifact_path": args.artifact, "sha256": args.sha256, "target_version": args.target_version}
if args.target_schema is not None: payload["target_schema"] = args.target_schema
if args.restore_data:
    payload.update({"restore_data": True, "backup_ref": args.backup_ref, "backup_sha256": args.backup_sha256})
request = Request(f"{args.base_url.rstrip('/')}/v1/updates/native/{args.operation}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
try:
    with urlopen(request, timeout=180) as response:
        print(response.status)
        print(response.read().decode())
except HTTPError as error:
    print(error.code)
    print(error.read().decode())
    raise SystemExit(1)
if args.wait_health:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urlopen(args.base_url.rstrip("/") + "/healthz", timeout=3) as response:
                health = json.load(response)
            if health.get("version") == args.target_version and health.get("status") == "ok":
                print(json.dumps(health, sort_keys=True))
                break
        except OSError:
            pass
        time.sleep(0.25)
    else:
        raise SystemExit("Timed out waiting for updated health")
