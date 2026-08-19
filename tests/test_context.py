from pathlib import Path

from virtizai_core.context import ContextBroker, MemoryService, estimate_tokens
from virtizai_core.db import Database
from virtizai_core.registries import EnvironmentRegistry, ProjectRegistry


def test_scoped_context_budget_excludes_unrelated_data(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    projects = ProjectRegistry(database)
    project_id = projects.create("Relevant")
    other_id = projects.create("Other")
    environment_id = EnvironmentRegistry(database).create("Relevant host", "local")
    database.execute("INSERT INTO project_environment_targets(project_id, environment_target_id) VALUES (?, ?)", (project_id, environment_id))
    memory = MemoryService(database)
    memory.add("relevant durable fact", "project", "user", project_id, importance=1.0)
    memory.add("unrelated durable fact", "other", "user", other_id, importance=1.0)
    memory.add("job scratch must not appear", "job", "user", project_id, memory_type="scratch", importance=1.0)
    bundle = ContextBroker(database).retrieve("user", "session", project_id)
    assert [item["content"] for item in bundle.memory] == ["relevant durable fact"]
    assert bundle.projects[0]["name"] == "Relevant"
    assert bundle.environments[0]["name"] == "Relevant host"
    assert bundle.used_tokens <= bundle.max_tokens
    database.close()


def test_secretary_context_is_minimal_and_estimated(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    memory = MemoryService(database)
    content = "small fact"
    memory.add(content, "user", "user")
    bundle = ContextBroker(database).secretary_context("user", "session")
    assert bundle["budget_name"] == "secretary"
    assert bundle["used_tokens"] == estimate_tokens(content)
    assert bundle["projects"] == []
    database.close()
