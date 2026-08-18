from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request

from virtizai_core.version import __version__


class ApiClient:
    def __init__(self, base_url: str): self.base_url = base_url.rstrip('/')
    def request(self, path: str, method: str = 'GET', payload: dict | None = None) -> dict | list:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, headers={'Content-Type':'application/json'}, method=method)
        with urllib.request.urlopen(request, timeout=60) as response: return json.loads(response.read().decode())


def print_json(value: object) -> None: print(json.dumps(value, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='virtizai')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--url', default=os.environ.get('VIRTIZAI_URL', 'http://127.0.0.1:8767'))
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('status','models','providers','roles','routes','jobs','projects','tools','releases'):
        sub.add_parser(name)
    chat = sub.add_parser('chat'); chat.add_argument('--session-id'); chat.add_argument('--project-id')
    sub.add_parser('update'); sub.add_parser('rollback')
    args = parser.parse_args(argv); client = ApiClient(args.url)
    paths = {'status':'/healthz','models':'/v1/models','providers':'/v1/providers','roles':'/v1/roles','routes':'/v1/routes','jobs':'/v1/jobs','projects':'/v1/projects','tools':'/v1/tools','releases':'/v1/releases'}
    if args.command in paths: print_json(client.request(paths[args.command])); return 0
    if args.command in ('update','rollback'):
        print(f'{args.command} is available through the shared Update Manager contract; backend implementation is pending.')
        return 0
    if args.command == 'chat':
        user_id = 'cli-user'; session_id = args.session_id
        if not session_id: session_id = client.request('/v1/sessions','POST',{'user_id':user_id})['session_id']
        print(f'Session: {session_id}. Ctrl-D to exit.')
        while True:
            try: content = input('> ')
            except EOFError: break
            if not content.strip(): continue
            response = client.request(f'/v1/sessions/{session_id}/messages','POST',{'user_id':user_id,'content':content})
            print(response.get('content',''))
            print(f"Model={response.get('model_name') or response.get('model_id')} Provider={response.get('provider_name') or response.get('provider_id')} Tokens={response.get('total_tokens')} TotalLatency={response.get('latency_ms'):.2f}ms")
        return 0
    return 0

if __name__ == '__main__': raise SystemExit(main())
