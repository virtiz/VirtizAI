import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig


def config_for(tmp_path: Path) -> AppConfig:
    return AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db")


@pytest.mark.asyncio
async def test_routes_require_explicit_enabled_role_and_are_mutable_without_session_affinity_changes(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT INTO roles(id, name, purpose, enabled, requirements_json) VALUES (?, ?, ?, ?, ?)", ("role-disabled-test", "disabled", "disabled", 0, "{}"))
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, capabilities_json, status, metadata_json) VALUES (?, ?, ?, ?, ?, ?)", ("model-test", "provider-test", "phi", "{}", "healthy", "{}"))
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user", "User"))
    db.execute("INSERT INTO sessions(id, user_id, affinity_provider_id, affinity_model_id, affinity_provider_name, affinity_model_name) VALUES (?, ?, ?, ?, ?, ?)", ("session-test", "user", "provider-test", "model-test", "Provider", "phi"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bad = await client.post("/v1/routes", json={"name": "bad", "role_id": "missing", "targets": []})
        assert bad.status_code == 422
        assert "does not exist" in bad.json()["detail"]
        disabled = await client.post("/v1/routes", json={"name": "disabled", "role_id": "role-disabled-test", "targets": []})
        assert disabled.status_code == 422
        assert "disabled" in disabled.json()["detail"]

        created = await client.post("/v1/routes", json={"name": "Secretary", "role_id": "role-secretary", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert created.status_code == 200
        route_id = created.json()["id"]
        assert created.json()["role_id"] == "role-secretary"
        rejected_update = await client.put(f"/v1/routes/{route_id}", json={"role_id": "role-disabled-test", "targets": []})
        assert rejected_update.status_code == 422
        assert "disabled" in rejected_update.json()["detail"]
        updated = await client.put(f"/v1/routes/{route_id}", json={"role_id": "role-coding", "strategy": "priority", "priority": 10, "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert updated.status_code == 200
        assert updated.json()["role_id"] == "role-coding"
        listed = await client.get("/v1/routes")
        assert listed.json()[0]["role_name"] == "coding"
        assert (await client.delete(f"/v1/routes/{route_id}")).status_code == 200
        assert (await client.get("/v1/routes")).json() == []

    session = db.fetch_one("SELECT affinity_provider_id, affinity_model_id FROM sessions WHERE id = ?", ("session-test",))
    assert dict(session) == {"affinity_provider_id": "provider-test", "affinity_model_id": "model-test"}


@pytest.mark.asyncio
async def test_routes_persist_validated_delegated_execution_selection(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, capabilities_json, status, metadata_json) VALUES (?, ?, ?, ?, ?, ?)", ("model-test", "provider-test", "phi", "{}", "healthy", "{}"))
    db.execute("INSERT INTO workers(id, name, worker_type, enabled, status, capabilities_json, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("worker-test", "Worker", "dev_tools", 1, "healthy", "[]", "{}"))
    db.execute("INSERT INTO environment_targets(id, name, target_type, enabled, status, capabilities_json, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("environment-test", "Environment", "workspace", 1, "healthy", "[]", "{}"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {"name": "Coding", "role_id": "role-coding", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}], "delegated_execution": {"worker_id": "worker-test", "environment_id": "environment-test"}}
        created = await client.post("/v1/routes", json=payload)
        assert created.status_code == 200
        route_id = created.json()["id"]
        assert created.json()["delegated_execution"] == payload["delegated_execution"]
        listed = await client.get("/v1/routes")
        assert listed.status_code == 200
        assert listed.json()[0]["delegated_execution"] == payload["delegated_execution"]

        preserved = await client.put(f"/v1/routes/{route_id}", json={"strategy": "priority", "targets": payload["targets"]})
        assert preserved.status_code == 200
        assert preserved.json()["delegated_execution"] == payload["delegated_execution"]

        rejected = await client.put(f"/v1/routes/{route_id}", json={"strategy": "priority", "targets": payload["targets"], "delegated_execution": {"worker_id": "missing", "environment_id": "environment-test"}})
        assert rejected.status_code == 422
        assert "worker" in rejected.json()["detail"].lower()

        cleared = await client.put(f"/v1/routes/{route_id}", json={"strategy": "priority", "targets": payload["targets"], "delegated_execution": None})
        assert cleared.status_code == 200
        assert cleared.json()["delegated_execution"] is None


@pytest.mark.asyncio
async def test_delegated_execution_route_validation_and_policy_merge(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, capabilities_json, status, metadata_json) VALUES (?, ?, ?, ?, ?, ?)", ("model-test", "provider-test", "phi", "{}", "healthy", "{}"))
    for worker_id, enabled in (("worker-one", 1), ("worker-two", 1), ("worker-disabled", 0)):
        db.execute("INSERT INTO workers(id, name, worker_type, enabled, status, capabilities_json, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", (worker_id, worker_id, "dev_tools", enabled, "healthy", "[]", "{}"))
    for environment_id, enabled in (("environment-one", 1), ("environment-two", 1), ("environment-disabled", 0)):
        db.execute("INSERT INTO environment_targets(id, name, target_type, enabled, status, capabilities_json, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", (environment_id, environment_id, "workspace", enabled, "healthy", "[]", "{}"))

    targets = [{"provider_id": "provider-test", "model_id": "model-test", "ordinal": 0}]
    first = {"worker_id": "worker-one", "environment_id": "environment-one"}
    second = {"worker_id": "worker-two", "environment_id": "environment-two"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/routes", json={"name": "Coding", "role_id": "role-coding", "strategy": "balanced", "priority": 7, "targets": targets, "delegated_execution": first})
        assert created.status_code == 200
        route_id = created.json()["id"]
        db.execute("UPDATE routes SET policy_json=? WHERE id=?", (json.dumps({"strategy": "balanced", "retained_policy": "keep", "delegated_execution": first}), route_id))

        updated = await client.put(f"/v1/routes/{route_id}", json={"strategy": "lowest_latency", "priority": 4, "targets": targets, "delegated_execution": second})
        assert updated.status_code == 200
        listed = (await client.get("/v1/routes")).json()[0]
        assert listed["delegated_execution"] == second
        assert listed["strategy"] == "lowest_latency"
        assert listed["priority"] == 4
        assert listed["targets"][0]["model_id"] == "model-test"
        policy = json.loads(db.fetch_one("SELECT policy_json FROM routes WHERE id=?", (route_id,))["policy_json"])
        assert policy["retained_policy"] == "keep"

        for execution, expected in (
            ({"worker_id": "missing", "environment_id": "environment-one"}, "worker"),
            ({"worker_id": "worker-one", "environment_id": "missing"}, "environment"),
            ({"worker_id": "worker-disabled", "environment_id": "environment-one"}, "worker"),
            ({"worker_id": "worker-one", "environment_id": "environment-disabled"}, "environment"),
        ):
            response = await client.put(f"/v1/routes/{route_id}", json={"strategy": "lowest_latency", "priority": 4, "targets": targets, "delegated_execution": execution})
            assert response.status_code == 422
            assert expected in response.json()["detail"].lower()

        partial = await client.put(f"/v1/routes/{route_id}", json={"strategy": "lowest_latency", "priority": 4, "targets": targets, "delegated_execution": {"worker_id": "worker-one"}})
        assert partial.status_code == 422


@pytest.mark.asyncio
async def test_routes_include_targets_in_list(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-secretary", "secretary", "Fast conversational secretary", 1))
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, status) VALUES (?, ?, ?, ?)", ("model-test", "provider-test", "phi", "available"))
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user", "User"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/routes", json={"name": "Secretary Route", "role_id": "role-secretary", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        route_id = created.json()["id"]

        listed = await client.get("/v1/routes")
        routes = listed.json()
        assert len(routes) == 1
        assert "targets" in routes[0]
        assert len(routes[0]["targets"]) == 1
        assert routes[0]["targets"][0]["model_id"] == "model-test"

        await client.delete(f"/v1/routes/{route_id}")


@pytest.mark.asyncio
async def test_put_without_role_id_preserves_existing_role(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-secretary", "secretary", "Fast conversational secretary", 1))
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, status) VALUES (?, ?, ?, ?)", ("model-test", "provider-test", "phi", "available"))
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user", "User"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/routes", json={"name": "Secretary Route", "role_id": "role-secretary", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        route_id = created.json()["id"]

        updated = await client.put(f"/v1/routes/{route_id}", json={"strategy": "lowest_latency", "priority": 5, "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert updated.status_code == 200
        assert updated.json()["role_id"] == "role-secretary"

        await client.delete(f"/v1/routes/{route_id}")


@pytest.mark.asyncio
async def test_put_nonexistent_route_returns_404(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.put("/v1/routes/nonexistent-id", json={"strategy": "priority"})
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_nonexistent_route_returns_404(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/v1/routes/nonexistent-id")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_eligibility_uses_persisted_route_role(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-secretary", "secretary", "Fast conversational secretary", 1))
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, status) VALUES (?, ?, ?, ?)", ("model-test", "provider-test", "phi", "available"))
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user", "User"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/routes", json={"name": "Secretary Route", "role_id": "role-secretary", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert created.status_code == 200
        route_id = created.json()["id"]

        eligibility = await client.get(f"/v1/routes/{route_id}/eligibility")
        assert eligibility.status_code == 200
        data = eligibility.json()

        assert "routes" in data, "Eligibility must return routes key"
        assert len(data["routes"]) >= 1, "Eligibility must return at least one candidate for secretary route"

        first_candidate = data["routes"][0]
        assert "route_id" in first_candidate, "Candidate must include route_id"
        assert first_candidate["route_id"] == route_id, f"Candidate route_id {first_candidate['route_id']} must match created route {route_id}"
        assert first_candidate["provider_id"] == "provider-test", "Candidate must reference the seeded provider"
        assert first_candidate["model_id"] == "model-test", "Candidate must reference the seeded model"

        await client.delete(f"/v1/routes/{route_id}")


@pytest.mark.asyncio
async def test_non_secretary_routes_excluded_from_secretary_resolution(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-secretary", "secretary", "Fast conversational secretary", 1))
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-coding", "coding", "Coding assistant", 1))
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, status) VALUES (?, ?, ?, ?)", ("model-test", "provider-test", "phi", "available"))
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user", "User"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/routes", json={"name": "Coding Route", "role_id": "role-coding", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert created.status_code == 200
        coding_route_id = created.json()["id"]

        sec_created = await client.post("/v1/routes", json={"name": "Secretary Route", "role_id": "role-secretary", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert sec_created.status_code == 200
        sec_route_id = sec_created.json()["id"]

        from virtizai_core.services import RouteResolver
        resolver = RouteResolver(db)
        secretary_routes = resolver.resolve_secretary()

        secretary_route_ids = [r.route_id for r in secretary_routes]

        assert coding_route_id not in secretary_route_ids, "Coding route (role-coding) must be excluded from secretary resolution"
        assert sec_route_id in secretary_route_ids, "Canonical role-secretary route must be present when it has eligible targets"

        canonical_found = [r for r in secretary_routes if r.route_id == sec_route_id]
        assert len(canonical_found) >= 1, "Must find at least one secretary route entry"

        await client.delete(f"/v1/routes/{coding_route_id}")
        await client.delete(f"/v1/routes/{sec_route_id}")


@pytest.mark.asyncio
async def test_canonical_role_secretary_used_for_resolution(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-secretary", "secretary", "Fast conversational secretary", 1))
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-other-secretary", "secretary-alt", "Other secretary role", 1))
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, status) VALUES (?, ?, ?, ?)", ("model-test", "provider-test", "phi", "available"))
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user", "User"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/routes", json={"name": "Other Secretary Route", "role_id": "role-other-secretary", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert created.status_code == 200
        other_route_id = created.json()["id"]

        sec_created = await client.post("/v1/routes", json={"name": "Canonical Secretary Route", "role_id": "role-secretary", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert sec_created.status_code == 200
        sec_route_id = sec_created.json()["id"]

        from virtizai_core.services import RouteResolver
        resolver = RouteResolver(db)
        secretary_routes = resolver.resolve_secretary()

        secretary_route_ids = [r.route_id for r in secretary_routes]

        assert other_route_id not in secretary_route_ids, "Non-canonical role-other-secretary route must be excluded from secretary resolution"
        assert sec_route_id in secretary_route_ids, "Canonical role-secretary route must be returned by resolve_secretary()"

        canonical_entries = [r for r in secretary_routes if r.route_id == sec_route_id]
        assert len(canonical_entries) >= 1, "Must find canonical secretary route entries"

        for entry in secretary_routes:
            assert entry.route_id != other_route_id, f"Non-canonical route {other_route_id} must not appear in secretary resolution"

        await client.delete(f"/v1/routes/{other_route_id}")
        await client.delete(f"/v1/routes/{sec_route_id}")


@pytest.mark.asyncio
async def test_similarly_named_roles_not_satisfied_for_readiness(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-secretary", "secretary", "Fast conversational secretary", 1))
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-other-secretary", "secretary-alt", "Other secretary role", 1))
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, status) VALUES (?, ?, ?, ?)", ("model-test", "provider-test", "phi", "available"))
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user", "User"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/routes", json={"name": "Other Secretary Route", "role_id": "role-other-secretary", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert created.status_code == 200
        other_route_id = created.json()["id"]

        readiness = await client.get("/v1/readiness")
        assert readiness.status_code == 200, f"Readiness endpoint returned {readiness.status_code}: {readiness.text}"
        data = readiness.json()

        assert "secretary" in data, "Readiness must include secretary key"
        secretary_status = data["secretary"]

        assert secretary_status.get("configured") is False, f"Route with non-canonical role_id (role-other-secretary) must NOT configure secretary. Readiness: {data}"
        assert secretary_status.get("ready") is False, f"Route with non-canonical role_id (role-other-secretary) must NOT make secretary ready. Readiness: {data}"
        assert secretary_status.get("route_id") is None, f"No canonical route ID should be reported. Readiness: {data}"

        await client.delete(f"/v1/routes/{other_route_id}")


@pytest.mark.asyncio
async def test_readiness_with_canonical_secretary_route_satisfied(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-secretary", "secretary", "Fast conversational secretary", 1))
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, status) VALUES (?, ?, ?, ?)", ("model-test", "provider-test", "phi", "available"))
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user", "User"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/routes", json={"name": "Secretary Route", "role_id": "role-secretary", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert created.status_code == 200
        route_id = created.json()["id"]

        readiness = await client.get("/v1/readiness")
        assert readiness.status_code == 200, f"Readiness endpoint returned {readiness.status_code}: {readiness.text}"
        data = readiness.json()

        secretary_status = data["secretary"]

        assert secretary_status.get("configured") is True, f"Canonical role-secretary route must configure secretary. Readiness: {data}"
        assert secretary_status.get("ready") is True, f"Canonical role-secretary route must make secretary ready. Readiness: {data}"
        assert secretary_status.get("route_id") == route_id, f"Readiness should report the canonical route ID. Readiness: {data}"

        await client.delete(f"/v1/routes/{route_id}")


@pytest.mark.asyncio
async def test_route_crud_does_not_alter_session_affinity(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    db = app.state.database
    db.execute("INSERT OR IGNORE INTO roles(id, name, purpose, enabled) VALUES (?, ?, ?, ?)", ("role-secretary", "secretary", "Fast conversational secretary", 1))
    db.execute("INSERT INTO providers(id, name, adapter_type, endpoint, enabled, health_status, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)", ("provider-test", "Provider", "ollama", "http://example", 1, "healthy", "{}"))
    db.execute("INSERT INTO models(id, provider_id, name, status) VALUES (?, ?, ?, ?)", ("model-test", "provider-test", "phi", "available"))
    db.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", ("user", "User"))
    db.execute("INSERT INTO sessions(id, user_id, affinity_provider_id, affinity_model_id) VALUES (?, ?, ?, ?)", ("session-test", "user", "provider-test", "model-test"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/routes", json={"name": "Secretary Route", "role_id": "role-secretary", "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        route_id = created.json()["id"]

        updated = await client.put(f"/v1/routes/{route_id}", json={"role_id": "role-coding", "strategy": "priority", "priority": 10, "targets": [{"provider_id": "provider-test", "model_id": "model-test"}]})
        assert updated.status_code == 200

        await client.delete(f"/v1/routes/{route_id}")

        session = db.fetch_one("SELECT affinity_provider_id, affinity_model_id FROM sessions WHERE id = ?", ("session-test",))
        assert dict(session) == {"affinity_provider_id": "provider-test", "affinity_model_id": "model-test"}
