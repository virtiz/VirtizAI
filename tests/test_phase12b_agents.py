from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from virtizai_core.api import create_app
from virtizai_core.config import AppConfig

@pytest.mark.asyncio
async def test_agents_workers_jobs_and_environment_crud(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VIRTIZAI_CODEX_BIN", "/bin/true")
    config = AppConfig(tmp_path/"data", tmp_path/"workspace", tmp_path/"logs", tmp_path/"data"/"state.db")
    app = create_app(config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        agents = await client.get("/v1/agents")
        assert agents.status_code == 200
        assert isinstance(agents.json(), list)
        workers = await client.get("/v1/workers")
        assert workers.status_code == 200
        assert workers.json()[0]["name"] == "Codex CLI"
        assert all("token" not in str(worker).lower() for worker in workers.json())
        assert (await client.get("/v1/jobs")).status_code == 200
        created = await client.post("/v1/environments", json={"name":"Synthetic QA","target_type":"test","address":"test://local","credential_ref":None,"capabilities":["read"]})
        assert created.status_code == 200
        target_id = created.json()["id"]
        assert any(item["id"] == target_id for item in (await client.get("/v1/environments")).json())
        assert (await client.delete(f"/v1/environments/{target_id}")).status_code == 200
        assert not any(item["id"] == target_id for item in (await client.get("/v1/environments")).json())

@pytest.mark.asyncio
async def test_project_crud_session_and_environment_associations(tmp_path: Path):
    app = create_app(AppConfig(tmp_path/'data', tmp_path/'workspace', tmp_path/'logs', tmp_path/'data'/'state.db'))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        project = (await client.post('/v1/projects', json={'name':'QA Project','objective':'Validate customer flow','status':'active'})).json()
        env = (await client.post('/v1/environments', json={'name':'QA Environment','target_type':'test','address':'test://local'})).json()
        session = (await client.post('/v1/sessions', json={'user_id':'qa','title':'QA Chat'})).json()
        linked = await client.patch(f"/v1/sessions/{session['session_id']}/project", json={'project_id':project['id']})
        assert linked.json()['project_id'] == project['id']
        assert (await client.post(f"/v1/projects/{project['id']}/environments", json={'environment_target_id':env['id']})).status_code == 200
        detail = (await client.get(f"/v1/projects/{project['id']}")).json()
        assert detail['objective'] == 'Validate customer flow'
        assert detail['sessions'][0]['id'] == session['session_id']
        assert detail['environments'][0]['id'] == env['id']
        updated = (await client.patch(f"/v1/projects/{project['id']}", json={'status':'archived'})).json()
        assert updated['status'] == 'archived'
        assert (await client.delete(f"/v1/projects/{project['id']}")).status_code == 200
