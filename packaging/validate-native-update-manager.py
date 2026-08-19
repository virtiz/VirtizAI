#!/usr/bin/env python3
"""Call the local native Update Manager API with typed JSON and print its response."""
from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--artifact", required=True)
parser.add_argument("--sha256", required=True)
parser.add_argument("--target-version", required=True)
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
