from pathlib import Path

from virtizai_core.context import ContextBroker, MemoryService
from virtizai_core.db import Database
from virtizai_core.registries import EnvironmentRegistry, ProjectRegistry


def test_project_switch_and_memory_scope_and_supersession(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    projects = ProjectRegistry(database)
    first = projects.create("First")
    second = projects.create("Second")
    env_first = EnvironmentRegistry(database).create("first-host", "host")
    env_second = EnvironmentRegistry(database).create("second-host", "host")
    database.execute("INSERT INTO project_environment_targets VALUES (?, ?, 'relevant')", (first, env_first))
    database.execute("INSERT INTO project_environment_targets VALUES (?, ?, 'relevant')", (second, env_second))
    memory = MemoryService(database)
    old = memory.add("old fact", "project", "user", first, importance=1)
    new = memory.add("new fact", "project", "user", first, importance=1)
    database.execute("UPDATE memory_items SET superseded_by = ? WHERE id = ?", (new, old))
    memory.add("second fact", "project", "user", second, importance=1)
    broker = ContextBroker(database)
    first_context = broker.retrieve("user", "session", first)
    second_context = broker.retrieve("user", "session", second)
    assert [item["content"] for item in first_context.memory] == ["new fact"]
    assert first_context.environments[0]["name"] == "first-host"
    assert second_context.environments[0]["name"] == "second-host"
    assert all(item["content"] != "second fact" for item in first_context.memory)
    database.close()


def test_tool_selection_and_budget_telemetry(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    broker = ContextBroker(database)
    tools = [{"name": "service_status", "description": "small"}]
    bundle = broker.retrieve("user", "session", requested_sources=["conversation"], requested_tools=tools)
    assert bundle.tools == tools
    assert bundle.category_tokens["memory"] == 0
    assert "memory" in bundle.omitted_sources
    build = database.fetch_one("SELECT * FROM context_builds WHERE id = ?", (bundle.build_id,))
    assert build is not None
    assert '"tools":' in build["category_tokens_json"]
    database.close()


def test_secretary_aggregate_budget_is_enforced(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    memory = MemoryService(database)
    memory.add("x" * 20_000, "global", "user", importance=1)
    bundle = ContextBroker(database).retrieve("user", "session")
    assert bundle.used_tokens <= bundle.max_tokens
    assert "memory" in bundle.omitted_sources
    database.close()
