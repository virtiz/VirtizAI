#!/usr/bin/env python3
"""Print package, health, update history, and backup state for release validation."""
from __future__ import annotations
import argparse, json, os, sqlite3, subprocess
from urllib.request import urlopen
parser=argparse.ArgumentParser()
parser.add_argument('--base-url', default=os.environ.get('VIRTIZAI_BASE_URL','http://127.0.0.1:8766'))
parser.add_argument('--database', default=os.environ.get('VIRTIZAI_DATABASE','/var/lib/virtizai/virtizai.db'))
args=parser.parse_args()
package=subprocess.check_output(['dpkg-query','-W','virtizai'], text=True).strip()
with urlopen(args.base_url.rstrip('/')+'/healthz', timeout=5) as response: health=json.load(response)
database=sqlite3.connect(args.database)
history=database.execute('SELECT version, action, status, metadata_json FROM update_history ORDER BY created_at DESC LIMIT 5').fetchall()
backups=database.execute('SELECT backup_ref, checksum_sha256, verified FROM update_backups ORDER BY created_at DESC LIMIT 5').fetchall()
print(json.dumps({'package':package,'health':health,'history':history,'backups':backups}, indent=2))
