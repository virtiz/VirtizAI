#!/usr/bin/env python3
"""Generate a checksum-bound release manifest from local VirtizAI artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--channel", default="stable", choices=("stable", "beta", "nightly"))
    parser.add_argument("--release-url", required=True)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-schema", type=int, required=True)
    parser.add_argument("--minimum-upgrade-version", required=True)
    parser.add_argument("--rollback-requires-data-restore", action="store_true")
    parser.add_argument("--rollback-baseline", action="append", default=[])
    args = parser.parse_args()
    manifest = {
        "version": args.version,
        "channel": args.channel,
        "release_url": args.release_url,
        "artifacts": [{"platform": "debian-amd64", "url": args.deb.name, "sha256": sha256(args.deb)}],
        "classification": {"type": "bugfix", "severity": "low", "breaking": False},
        "minimum_upgrade_version": args.minimum_upgrade_version,
        "schema_compatibility": {"minimum": args.target_schema, "maximum": args.target_schema, "target": args.target_schema},
        "rollback_compatibility": {"supported": True, "requires_data_restore": args.rollback_requires_data_restore, "application_only_compatible": not args.rollback_requires_data_restore, "compatible_baselines": args.rollback_baseline},
    }
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())