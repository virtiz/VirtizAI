from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.workers import TaskClassifier, CodexWorker, ManagedCodingWorkerExecutor, ExecutionRequest, WorkerExecutionBoundary


def test_task_classifier_defaults_and_config(monkeypatch):
    classifier = TaskClassifier()
    assert classifier.classify("hello there").kind == "simple"
    assert classifier.classify("analyze this architecture").kind == "medium"
    assert classifier.classify("implement a fix in the repository").kind == "hard"
    monkeypatch.setenv("VIRTIZAI_TASK_CLASSIFIER_CONFIG", '{"medium_keywords":["ponderx"]}')
    assert TaskClassifier().classify("ponderx this").kind == "medium"


@pytest.mark.asyncio
async def test_codex_worker_normalizes_result_and_restricts_workspace(tmp_path, monkeypatch):
    fake = tmp_path.parent / "fake-codex"
    fake.write_text("#!/bin/sh\necho ok\n")
    fake.chmod(0o755)
    monkeypatch.setenv("VIRTIZAI_CODEX_BIN", str(fake))
    result = await CodexWorker(tmp_path).run("job-1", {"prompt": "safe task"})
    assert result["worker"] == "codex"
    assert result["status"] == "succeeded"
    with pytest.raises(PermissionError):
        await CodexWorker(tmp_path).run("job-2", {"prompt": "x", "workspace": "/tmp/outside"})

@pytest.mark.asyncio
async def test_managed_coding_worker_uses_explicit_sandbox_and_repo_evidence(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir();(workspace/'README.md').write_text('hello\n')
    import subprocess; subprocess.run(['git','init'],cwd=workspace,check=True,stdout=subprocess.DEVNULL);subprocess.run(['git','config','user.email','test@example.invalid'],cwd=workspace,check=True);subprocess.run(['git','config','user.name','Test'],cwd=workspace,check=True);subprocess.run(['git','add','.'],cwd=workspace,check=True);subprocess.run(['git','commit','-m','initial'],cwd=workspace,check=True,stdout=subprocess.DEVNULL)
    fake=tmp_path/'fake-codex';fake.write_text('#!/bin/sh\necho "{\\"item\\":{\\"type\\":\\"agent_message\\",\\"text\\":\\"bounded result\\"}}"\n');fake.chmod(0o755)
    from virtizai_core.db import Database
    db=Database(tmp_path/'managed.db');db.open()
    db.execute("INSERT INTO workers(id,name,worker_type,config_json) VALUES('w','W','managed_coding',?)", ('{"executable":"'+str(fake)+'","timeout_seconds":5}',))
    db.execute("INSERT INTO environment_targets(id,name,target_type,config_json) VALUES('e','E','workspace',?)", ('{"workspace_path":"'+str(workspace)+'","allowed_worker_types":["managed_coding"]}',))
    boundary=WorkerExecutionBoundary(db);boundary.register(ManagedCodingWorkerExecutor())
    result=await boundary.execute(ExecutionRequest('w','e','managed_coding',{'objective':'inspect README','write_authorized':False}))
    assert result.status=='succeeded' and result.output['sandbox']=='read-only' and result.output['files_changed']==[]
    db.close()


@pytest.mark.asyncio
async def test_hard_request_is_async_job_and_medium_is_explicitly_unavailable(tmp_path: Path):
    config = AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db")
    app = create_app(config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sid = (await client.post("/v1/sessions", json={"user_id": "u"})).json()["session_id"]
        hard = await client.post(f"/v1/sessions/{sid}/messages", json={"user_id": "u", "content": "implement a harmless test"})
        assert hard.json()["job_created"] is True
        assert "Codex worker job accepted" in hard.json()["content"]
        medium = await client.post(f"/v1/sessions/{sid}/messages", json={"user_id": "u", "content": "analyze this architecture"})
        assert "Medium route unavailable" in medium.json()["content"]
        await app.state.jobs.wait_for_idle()


def test_introspection_is_deterministic_and_secret_free(tmp_path, monkeypatch):
    from virtizai_core.db import Database
    from virtizai_core.services import IntrospectionService
    db = Database(tmp_path / "state.db")
    db.open()
    service = IntrospectionService(db, tmp_path / "workspace")
    assert service.matches("what is your current routing configuration?")
    rendered = service.render()
    assert "Current VirtizAI routing configuration" in rendered
    assert "API_KEY" not in rendered
    assert "token" not in rendered.lower()
