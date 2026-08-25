from __future__ import annotations

from pathlib import Path

import pytest

from virtizai_core.db import Database
from virtizai_core.health import HealthManager
from virtizai_core.providers import ProviderRegistry
from virtizai_core.routing import RoutingEngine
from virtizai_core.capability_routing import requirements_for


async def setup_provider(database: Database, name: str, fail: bool = False) -> tuple[str, str]:
    registry = ProviderRegistry(database)
    provider_id = registry.install_mock_provider(name, ["shared-model"], fail=fail)
    await registry.discover_models(provider_id)
    model_id = database.fetch_one("SELECT id FROM models WHERE provider_id = ?", (provider_id,))["id"]
    return provider_id, model_id


def setup_route(database: Database, provider_id: str, model_id: str, ordinal: int, priority: int = 100) -> str:
    route_id = f"route-{provider_id}"
    database.execute("INSERT OR IGNORE INTO routes(id, name, role_id, priority, policy_json) VALUES (?, ?, 'role-secretary', ?, '{\"strategy\":\"priority\"}')", (route_id, route_id, priority))
    database.execute("INSERT OR IGNORE INTO route_targets(route_id, provider_id, model_id, ordinal) VALUES (?, ?, ?, ?)", (route_id, provider_id, model_id, ordinal))
    return route_id


@pytest.mark.asyncio
async def test_same_model_name_is_distinct_per_provider_and_discovery(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    first, first_model = await setup_provider(database, "Provider One")
    second, second_model = await setup_provider(database, "Provider Two")
    assert first != second
    assert first_model != second_model
    assert database.fetch_one("SELECT COUNT(*) FROM models WHERE name = 'shared-model'")[0] == 2
    database.close()


@pytest.mark.asyncio
async def test_hysteresis_removes_and_restores_provider(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    registry = ProviderRegistry(database)
    provider_id = registry.install_mock_provider("Flapping Provider", ["model"], fail=True)
    await registry.discover_models(provider_id) if False else None
    health = HealthManager(database, registry.adapters)
    for _ in range(2):
        await health.check_provider(provider_id)
    assert database.fetch_one("SELECT health_status FROM providers WHERE id = ?", (provider_id,))["health_status"] == "unknown"
    await health.check_provider(provider_id)
    assert database.fetch_one("SELECT health_status FROM providers WHERE id = ?", (provider_id,))["health_status"] == "unavailable"
    registry.adapters[provider_id].fail = False
    await health.check_provider(provider_id)
    assert database.fetch_one("SELECT health_status FROM providers WHERE id = ?", (provider_id,))["health_status"] == "unavailable"
    await health.check_provider(provider_id)
    assert database.fetch_one("SELECT health_status FROM providers WHERE id = ?", (provider_id,))["health_status"] == "healthy"
    database.close()


@pytest.mark.asyncio
async def test_unavailable_provider_leaves_eligible_pool_and_fallback_warns(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    first, first_model = await setup_provider(database, "First")
    second, second_model = await setup_provider(database, "Second")
    database.execute("UPDATE providers SET health_status = 'healthy'")
    route_id = setup_route(database, first, first_model, 0)
    database.execute("INSERT INTO route_targets(route_id, provider_id, model_id, ordinal) VALUES (?, ?, ?, 1)", (route_id, second, second_model))
    routes = RoutingEngine(database).eligible_routes("role-secretary")
    assert [route.provider_id for route in routes] == [first, second]
    database.execute("UPDATE providers SET health_status = 'unavailable' WHERE id = ?", (first,))
    routes = RoutingEngine(database).eligible_routes("role-secretary")
    assert [route.provider_id for route in routes] == [second]
    second_model_two = "second-model-two"
    database.execute("INSERT INTO models(id, provider_id, name, status) VALUES (?, ?, ?, 'available')", (second_model_two, second, second_model_two))
    database.execute("INSERT INTO route_targets(route_id, provider_id, model_id, ordinal) VALUES (?, ?, ?, 2)", (route_id, second, second_model_two))
    warnings = RoutingEngine(database).warnings(RoutingEngine(database).eligible_routes("role-secretary"))
    assert warnings
    database.close()


@pytest.mark.asyncio
async def test_mock_inference_returns_real_metadata(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    registry = ProviderRegistry(database)
    provider_id = registry.install_mock_provider("Inference Provider", ["model"])
    result = await registry.chat(provider_id, "model", [{"role": "user", "content": "hello"}])
    assert result.content.startswith("Inference Provider")
    assert result.total_tokens == 8
    assert result.usage_exact is True
    database.close()


def test_capability_routing_filters_and_ranks_deterministically(tmp_path: Path) -> None:
    db=Database(tmp_path/'cap.db');db.open()
    db.execute("INSERT INTO providers(id,name,adapter_type,health_status) VALUES('p1','P1','mock','healthy'),('p2','P2','mock','healthy'),('p3','P3','mock','unavailable')")
    evidence='{"capability_evidence":{"chat":"verified","native_tool_calls":"verified","coding":"verified"}}'
    db.execute("INSERT INTO models(id,provider_id,name,status,locality,relative_cost,user_overrides_json) VALUES('m1','p1','one','available','remote',1,?),('m2','p2','two','available','local',2,?),('m3','p3','three','available','local',0,?)",(evidence,evidence,evidence))
    db.execute("INSERT INTO routes(id,name,role_id,priority,policy_json) VALUES('r','R','role-coding',10,'{\"capability_routing\":{\"enforce\":true}}')")
    for ordinal,provider,model in [(0,'p1','m1'),(1,'p2','m2'),(2,'p3','m3')]: db.execute("INSERT INTO route_targets(route_id,provider_id,model_id,ordinal) VALUES('r',?,?,?)",(provider,model,ordinal))
    decision=RoutingEngine(db).capability_selection('role-coding',requirements_for('role-coding'))
    assert decision['selected']['model_id']=='m2' and any(x['reason']=='provider_unhealthy' for x in decision['excluded']) and len(decision['fallback_candidates'])==1
    db.execute("UPDATE models SET user_overrides_json='{}' WHERE id='m2'")
    decision=RoutingEngine(db).capability_selection('role-coding');assert decision['selected']['model_id']=='m1';assert any(x['model_id']=='m2' and x['reason']=='capability_missing' for x in decision['excluded']);db.close()

def test_coding_managed_worker_is_eligible_without_native_tool_calls(tmp_path: Path) -> None:
    db=Database(tmp_path/'managed.db');db.open()
    db.execute("INSERT INTO providers(id,name,adapter_type,health_status) VALUES('p','P','mock','healthy')")
    evidence='{"capability_evidence":{"coding":"verified","managed_coding_worker":"verified"}}'
    db.execute("INSERT INTO models(id,provider_id,name,status,user_overrides_json) VALUES('m','p','managed','available',?)",(evidence,))
    db.execute("INSERT INTO routes(id,name,role_id,priority,policy_json) VALUES('r','R','role-coding',10,'{\"capability_routing\":{\"enforce\":true}}')")
    db.execute("INSERT INTO route_targets(route_id,provider_id,model_id,ordinal,conditions_json) VALUES('r','p','m',0,?)", ('{"execution_plan":"managed_coding_worker"}',))
    decision=RoutingEngine(db).capability_selection('role-coding')
    assert decision['selected']['execution_plan']=='managed_coding_worker'
    db.close()
