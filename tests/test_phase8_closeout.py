from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.costs import CostService
from virtizai_core.db import Database
from virtizai_core.retention import RetentionService


def test_cost_local_and_unknown_are_explicit(tmp_path: Path) -> None:
    db=Database(tmp_path/'state.db'); db.open(); db.execute("INSERT INTO providers(id,name,adapter_type) VALUES ('p','local','ollama')")
    service=CostService(db); local=service.calculate('p','m',10,20); assert local.local is True and local.amount is None
    db.execute("INSERT INTO providers(id,name,adapter_type) VALUES ('c','cloud','cloud')")
    assert service.calculate('c','m',10,20).source == 'unknown'; db.close()


def test_retention_prunes_telemetry_not_content(tmp_path: Path) -> None:
    db=Database(tmp_path/'state.db'); db.open(); db.execute("INSERT INTO users(id,display_name) VALUES ('u','u')"); db.execute("INSERT INTO sessions(id,user_id) VALUES ('s','u')"); db.execute("INSERT INTO messages(id,session_id,role,content) VALUES ('m','s','user','keep')"); db.execute("INSERT INTO memory_items(id,user_id,namespace,content) VALUES ('mem','u','global','keep memory')")
    old=(datetime.now(timezone.utc)-timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S'); db.execute("INSERT INTO telemetry_events(request_id,event_type,stage,created_at) VALUES ('r','request_stage','x',?)",(old,)); RetentionService(db).prune(); assert db.fetch_one("SELECT COUNT(*) FROM telemetry_events")[0]==0; assert db.fetch_one("SELECT content FROM messages")[0]=='keep'; assert db.fetch_one("SELECT content FROM memory_items")[0]=='keep memory'; db.close()


@pytest.mark.asyncio
async def test_tool_detail_modes_change_visible_response(tmp_path: Path) -> None:
    app=create_app(AppConfig(tmp_path/'data',tmp_path/'workspace',tmp_path/'logs',tmp_path/'data'/'state.db'))
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        hidden=(await client.post('/v1/tools/run',json={'tool':'host_status','tool_details':'hidden'})).json(); summary=(await client.post('/v1/tools/run',json={'tool':'host_status','tool_details':'summary'})).json(); commands=(await client.post('/v1/tools/run',json={'tool':'host_status','tool_details':'commands_results'})).json(); await __import__('asyncio').gather(*list(app.state.execution.active.values())); assert hidden['visible_detail'] is None; assert 'accepted' in summary['visible_detail']; assert commands['visible_detail']['tool']=='host_status'
