#!/usr/bin/env python3
"""Call the local native Update Manager API with typed JSON and print its response."""
from __future__ import annotations

import argparse
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--artifact", required=True)
parser.add_argument("--sha256", required=True)
parser.add_argument("--target-version", required=True)
parser.add_argument("--wait-health", action="store_true")
args = parser.parse_args()
payload = {"artifact_path": args.artifact, "sha256": args.sha256, "target_version": args.target_version}
request = Request("http://127.0.0.1:8766/v1/updates/native/apply", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
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
            with urlopen("http://127.0.0.1:8766/healthz", timeout=3) as response:
                health = json.load(response)
            if health.get("version") == args.target_version and health.get("status") == "ok":
                print(json.dumps(health, sort_keys=True))
                break
        except OSError:
            pass
        time.sleep(0.25)
    else:
        raise SystemExit("Timed out waiting for updated health")
