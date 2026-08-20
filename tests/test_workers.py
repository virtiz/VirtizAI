from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.workers import TaskClassifier, CodexWorker


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
