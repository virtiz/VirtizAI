#!/usr/bin/env python3
"""Set the canonical application version in a rehearsal checkout."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("version")
parser.add_argument("--path", type=Path, default=Path("virtizai_core/version.py"))
args = parser.parse_args()
text = args.path.read_text()
updated, count = re.subn(r'(__version__\s*=\s*")[^"]+(")', rf'\g<1>{args.version}\g<2>', text, count=1)
if count != 1:
    raise SystemExit("canonical version assignment not found exactly once")
args.path.write_text(updated)
