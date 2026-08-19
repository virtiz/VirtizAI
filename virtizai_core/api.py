from __future__ import annotations

import json
import uuid
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import AppConfig
from .context import ContextBroker, MemoryService
from .benchmark import benchmark_candidate
from .db import Database
from .auth import AuthAdminService
from .health import HealthManager
from .execution import ExecutionManager
from .tools import ToolRegistryService, ToolAuthorizationError
from .jobs import JobManager
from .providers import ProviderRegistry
from .registries import (
    EnvironmentRegistry,
    IntegrationRegistry,
    ProjectRegistry,
    ToolRegistry,
    UpdateManager,
)
from .services import CoreService
from .telemetry import TelemetryService
from .version import __version__
from .costs import CostService
from .retention import RetentionService
from .interfaces import InterfaceRequest, InterfaceService
from .discord import DiscordAdapter


class SessionCreate(BaseModel):
    user_id: str = Field(min_length=1)
    display_name: str = Field(default="User", min_length=1)
    title: str | None = None


class MessageCreate(BaseModel):
    user_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=100_000)
    display_name: str = Field(default="User", min_length=1)


class JobCreate(BaseModel):
    kind: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)
    user_id: str | None = None
    session_id: str | None = None


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1)
    adapter_type: str = Field(min_length=1)
    endpoint: str | None = None
    config: dict = Field(default_factory=dict)


class RoleCreate(BaseModel):
    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    requirements: dict = Field(default_factory=dict)


class RouteCreate(BaseModel):
    name: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    strategy: str = "priority"
    priority: int = 100
    targets: list[dict] = Field(default_factory=list)


class RouteUpdate(BaseModel):
    strategy: str = "priority"
    priority: int = 100
    targets: list[dict] = Field(default_factory=list)


class RouteTargetCreate(BaseModel):
    provider_id: str
    model_id: str
    ordinal: int = 0
    enabled: bool = True
    conditions: dict = Field(default_factory=dict)


class DemoFailure(BaseModel):
    provider_id: str
    fail: bool


class ToolRunRequest(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    profile: str = "secretary"
    user_id: str | None = None
    session_id: str | None = None
    tool_details: str = "summary"


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    root_path: str | None = None


class EnvironmentCreate(BaseModel):
    name: str
    target_type: str
    address: str | None = None
    credential_ref: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class MemoryCreate(BaseModel):
    content: str
    namespace: str
    user_id: str | None = None
    project_id: str | None = None
    memory_type: str = "durable"
    importance: float = 0.5
    source_ref: str | None = None
    confidence: float = 0.5
    verified_state: str = "unverified"


class InterfaceMessage(BaseModel):
    interface_type: str
    external_subject: str
    content: str
    session_id: str | None = None
    session_key: str | None = None
    display_name: str = "User"
    response_verbosity: str | None = None
    execution_updates: str | None = None
    tool_details: str | None = None


class DiscordConfigUpdate(BaseModel):
    enabled: bool = False
    mode: str = "existing_bot"
    bot_secret_ref: str | None = None
    allow_dms: bool = True
    require_mentions: bool = True
    slash_commands: bool = True
    dedicated_channel_id: str | None = None
    release_channel_id: str | None = None
    allowed_servers: list[str] = Field(default_factory=list)
    allowed_channels: list[str] = Field(default_factory=list)
    allowed_users: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    admin_users: list[str] = Field(default_factory=list)
    admin_roles: list[str] = Field(default_factory=list)


class CommunicationPreferences(BaseModel):
    user_id: str
    response_verbosity: str = "normal"
    execution_updates: str = "important_milestones"
    tool_details: str = "summary"
    interface_type: str | None = None


class IdentityLink(BaseModel):
    interface_type: str
    external_subject: str
    user_id: str
    display_name: str = "User"


class ReleaseManifestInput(BaseModel):
    manifest: dict


class UpdatePolicyInput(BaseModel):
    channel: str = "stable"
    version_policy: str = "follow_channel"
    pinned_version: str | None = None
    skipped_versions: list[str] = Field(default_factory=list)


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or AppConfig.from_environment()
    app_config.ensure_directories()
    app = FastAPI(title="VirtizAI Core", version=__version__)
    webui_dir = Path(__file__).resolve().parent.parent / "webui"
    app.mount("/static", StaticFiles(directory=webui_dir), name="static")
    database = Database(app_config.database_path)
    database.open()
    telemetry = TelemetryService(database)
    jobs = JobManager(database)
    providers = ProviderRegistry(database)
    providers.restore_adapters()
    core = CoreService(database, telemetry, jobs, providers)
    app.state.auth = AuthAdminService(database)
    app.state.context = ContextBroker(database)
    app.state.execution = ExecutionManager(database, app_config.workspace_dir)
    app.state.tools = ToolRegistryService(app.state.execution)
    app.state.health = HealthManager(database, providers.adapters)
    app.state.projects = ProjectRegistry(database)
    app.state.environments = EnvironmentRegistry(database)
    app.state.integrations = IntegrationRegistry(database)
    app.state.memory = MemoryService(database)
    app.state.updates = UpdateManager(database)
    app.state.costs = CostService(database)
    app.state.retention = RetentionService(database)
    app.state.config = app_config
    app.state.database = database
    app.state.jobs = jobs
    app.state.core = core
    app.state.interfaces = InterfaceService(database, core)
    app.state.discord = DiscordAdapter(app.state.interfaces)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await jobs.wait_for_idle()
        database.close()

    app.router.lifespan_context = lifespan

    @app.get("/healthz")
    async def healthz() -> dict:
        schema_version = database.fetch_one(
            "SELECT MAX(version) AS version FROM schema_migrations"
        )["version"]
        return {
            "status": "ok",
            "application": "VirtizAI",
            "version": app_config.app_version,
            "schema_version": schema_version,
        }

    @app.get("/v1/dashboard")
    async def dashboard() -> dict:
        latency_rows = [row["latency_ms"] for row in database.fetch_all("SELECT latency_ms FROM messages WHERE role = 'assistant' AND latency_ms IS NOT NULL ORDER BY created_at")]
        latency = database.fetch_one("SELECT COUNT(*) AS count FROM messages WHERE role = 'assistant' AND latency_ms IS NOT NULL")
        providers_summary = database.fetch_all(
            "SELECT health_status, COUNT(*) AS count FROM providers GROUP BY health_status"
        )
        jobs_summary = database.fetch_all(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
        )
        recent = database.fetch_all(
            "SELECT tool_id, driver, authorization, result_json, created_at FROM execution_audit ORDER BY created_at DESC LIMIT 5"
        )
        request_count = database.fetch_one("SELECT COUNT(*) AS count FROM messages WHERE role = 'user'")["count"]
        return {
            "request_count": request_count,
            "secretary_latency_ms": (sum(latency_rows) / len(latency_rows)) if latency_rows else None,
            "secretary_latency_p50_ms": latency_rows[(len(latency_rows) - 1) // 2] if latency_rows else None,
            "secretary_latency_p95_ms": latency_rows[max(0, int(len(latency_rows) * 0.95) - 1)] if latency_rows else None,
            "secretary_response_count": latency["count"],
            "providers": {row["health_status"]: row["count"] for row in providers_summary},
            "jobs": {row["status"]: row["count"] for row in jobs_summary},
            "recent_activity": [dict(row) for row in recent],
        }

    @app.get("/v1/releases")
    async def releases() -> dict:
        return {"releases": app.state.updates.releases(), "history": app.state.updates.history(), "policy": app.state.updates.policy()}

    @app.post("/v1/releases/import")
    async def import_release(request: ReleaseManifestInput) -> dict:
        try:
            return app.state.updates.import_manifest(request.manifest)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/updates/plan")
    async def update_plan(platform: str) -> dict:
        return app.state.updates.plan(app_config.app_version, platform)

    @app.get("/v1/updates/policy")
    async def update_policy() -> dict:
        return app.state.updates.policy()

    @app.put("/v1/updates/policy")
    async def put_update_policy(request: UpdatePolicyInput) -> dict:
        try:
            return app.state.updates.set_policy(request.channel, request.version_policy, request.pinned_version, request.skipped_versions)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/updates/{action}")
    async def request_update(action: str, platform: str) -> dict:
        if action not in {"update", "rollback"}:
            raise HTTPException(status_code=404, detail="Unknown update action")
        plan = app.state.updates.plan(app_config.app_version, platform)
        if action == "update" and not plan["available"]:
            raise HTTPException(status_code=409, detail="No verified update is available")
        version = plan.get("release", {}).get("version", app_config.app_version)
        update_id = app.state.updates.record(version, action, "planned", plan.get("artifact", {}).get("url"))
        return {"id": update_id, "action": action, "status": "planned", "plan": plan, "privilege_boundary": "Use a platform updater helper or an external deployment tool to apply this verified plan."}

    @app.post("/v1/telemetry/prune")
    async def prune_telemetry() -> dict:
        return app.state.retention.prune()

    @app.get("/v1/routing/explain/{role_id}")
    async def explain_routing(role_id: str, strategy: str = "priority") -> dict:
        from .routing import RoutingEngine
        return RoutingEngine(database).explain(role_id, strategy)

    @app.post("/v1/cost/estimate")
    async def estimate_cost(provider_id: str, model_name: str, input_tokens: int, output_tokens: int) -> dict:
        return app.state.costs.calculate(provider_id, model_name, input_tokens, output_tokens).__dict__

    @app.get("/v1/preferences/{user_id}")
    async def get_preferences(user_id: str, interface_type: str | None = None) -> dict:
        if interface_type:
            row = database.fetch_one("SELECT * FROM interface_preferences WHERE user_id = ? AND interface_type = ?", (user_id, interface_type))
        else:
            row = database.fetch_one("SELECT * FROM communication_preferences WHERE user_id = ?", (user_id,))
        return dict(row) if row else {"user_id": user_id, "response_verbosity": "normal", "execution_updates": "important_milestones", "tool_details": "summary"}

    @app.put("/v1/preferences")
    async def put_preferences(request: CommunicationPreferences) -> dict:
        database.execute("INSERT INTO users(id, display_name) VALUES (?, ?) ON CONFLICT(id) DO NOTHING", (request.user_id, request.user_id))
        if request.interface_type:
            database.execute("INSERT INTO interface_preferences(user_id, interface_type, response_verbosity, execution_updates, tool_details) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, interface_type) DO UPDATE SET response_verbosity=excluded.response_verbosity, execution_updates=excluded.execution_updates, tool_details=excluded.tool_details", (request.user_id, request.interface_type, request.response_verbosity, request.execution_updates, request.tool_details))
        else:
            database.execute("INSERT INTO communication_preferences(user_id, response_verbosity, execution_updates, tool_details) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET response_verbosity=excluded.response_verbosity, execution_updates=excluded.execution_updates, tool_details=excluded.tool_details", (request.user_id, request.response_verbosity, request.execution_updates, request.tool_details))
        return await get_preferences(request.user_id, request.interface_type)

    @app.post("/v1/interfaces/message")
    async def interface_message(request: InterfaceMessage) -> dict:
        session_id, response = await app.state.interfaces.handle(InterfaceRequest(**request.model_dump()))
        return {"session_id": session_id, **response.__dict__}

    @app.post("/v1/interfaces/link")
    async def link_interface_identity(request: IdentityLink) -> dict:
        try:
            app.state.interfaces.link_identity(request.interface_type, request.external_subject, request.user_id, request.display_name)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"linked": True}

    @app.get("/v1/interfaces/history")
    async def interface_history(interface_type: str, external_subject: str, session_id: str | None = None) -> list[dict]:
        return app.state.interfaces.history(interface_type, external_subject, session_id)

    @app.post("/v1/interfaces/stream")
    async def interface_stream(request: InterfaceMessage) -> StreamingResponse:
        session_id, response = await app.state.interfaces.handle(InterfaceRequest(**request.model_dump()))
        async def events():
            yield f"event: start\ndata: {json.dumps({'session_id': session_id})}\n\n"
            for chunk in [response.content[index:index + 32] for index in range(0, len(response.content), 32)]:
                yield f"event: token\ndata: {json.dumps({'content': chunk})}\n\n"
            yield f"event: complete\ndata: {json.dumps(response.__dict__)}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/v1/discord/message")
    async def discord_message(request: InterfaceMessage) -> dict:
        reply = await app.state.discord.handle_message(request.external_subject, request.content, request.session_key, request.session_id, request.display_name)
        return {"content": reply.content, "session_id": reply.session_id, "metadata": reply.metadata}

    @app.get("/v1/discord/command/{command}")
    async def discord_command(command: str, user_id: str) -> dict:
        return await app.state.discord.command(user_id, command)

    @app.get("/v1/discord/config")
    async def discord_config() -> dict:
        row = dict(database.fetch_one("SELECT * FROM discord_config WHERE id = 'discord-default'"))
        for field in ("bot_secret_ref", "enabled", "mode", "allow_dms", "require_mentions", "slash_commands", "dedicated_channel_id", "release_channel_id"):
            if field in {"bot_secret_ref", "dedicated_channel_id", "release_channel_id", "mode"}:
                continue
        for field in ("allowed_servers_json", "allowed_channels_json", "allowed_users_json", "allowed_roles_json", "admin_users_json", "admin_roles_json"):
            row[field.removesuffix("_json")] = json.loads(row.pop(field))
        row["bot_secret_configured"] = bool(row.get("bot_secret_ref"))
        row.pop("bot_secret_ref", None)
        return row

    @app.put("/v1/discord/config")
    async def update_discord_config(request: DiscordConfigUpdate) -> dict:
        values = request.model_dump()
        database.execute("""UPDATE discord_config SET enabled=?, mode=?, bot_secret_ref=?, allow_dms=?, require_mentions=?, slash_commands=?, dedicated_channel_id=?, release_channel_id=?, allowed_servers_json=?, allowed_channels_json=?, allowed_users_json=?, allowed_roles_json=?, admin_users_json=?, admin_roles_json=?, updated_at=CURRENT_TIMESTAMP WHERE id='discord-default'""", (int(values["enabled"]), values["mode"], values["bot_secret_ref"], int(values["allow_dms"]), int(values["require_mentions"]), int(values["slash_commands"]), values["dedicated_channel_id"], values["release_channel_id"], json.dumps(values["allowed_servers"]), json.dumps(values["allowed_channels"]), json.dumps(values["allowed_users"]), json.dumps(values["allowed_roles"]), json.dumps(values["admin_users"]), json.dumps(values["admin_roles"])))
        return await discord_config()

    @app.get("/")
    async def webui() -> FileResponse:
        return FileResponse(webui_dir / "index.html")

    @app.get("/v1/schema")
    async def schema() -> dict:
        tables = database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        return {"schema_version": database.fetch_one("SELECT MAX(version) AS version FROM schema_migrations")["version"], "tables": [row["name"] for row in tables]}

    @app.get("/v1/tools")
    async def list_tools() -> list[dict]:
        return app.state.tools.list_tools()

    @app.get("/v1/execution/drivers")
    async def execution_drivers() -> dict:
        return app.state.execution.driver_capabilities()

    @app.get("/v1/context")
    async def preview_context(user_id: str, session_id: str, project_id: str | None = None, query: str | None = None, budget: str = "secretary", sources: str | None = None) -> dict:
        requested = sources.split(",") if sources else None
        return app.state.context.retrieve(user_id, session_id, project_id, query, budget, requested).as_dict()

    @app.get("/v1/context/builds/{build_id}")
    async def context_build(build_id: str) -> dict:
        row = database.fetch_one("SELECT * FROM context_builds WHERE id = ?", (build_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="Context build not found")
        return dict(row)

    @app.get("/v1/projects")
    async def list_projects() -> list[dict]:
        return [dict(row) for row in database.fetch_all("SELECT * FROM projects ORDER BY name")]

    @app.post("/v1/projects")
    async def create_project(request: ProjectCreate) -> dict:
        project_id = app.state.projects.create(request.name, request.description, request.root_path)
        return dict(database.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,)))

    @app.get("/v1/environments")
    async def list_environments() -> list[dict]:
        return [dict(row) for row in database.fetch_all("SELECT * FROM environment_targets ORDER BY name")]

    @app.post("/v1/environments")
    async def create_environment(request: EnvironmentCreate) -> dict:
        target_id = app.state.environments.create(request.name, request.target_type, request.address, request.credential_ref)
        database.execute("UPDATE environment_targets SET capabilities_json = ? WHERE id = ?", (json.dumps(request.capabilities), target_id))
        return dict(database.fetch_one("SELECT * FROM environment_targets WHERE id = ?", (target_id,)))

    @app.get("/v1/memory")
    async def list_memory(user_id: str | None = None, project_id: str | None = None, query: str | None = None) -> list[dict]:
        rows = app.state.memory.list(user_id, project_id)
        return [row for row in rows if not query or query.lower() in row["content"].lower()]

    @app.post("/v1/memory")
    async def add_memory(request: MemoryCreate) -> dict:
        memory_id = app.state.memory.add(request.content, request.namespace, request.user_id, request.project_id, request.memory_type, request.importance, request.source_ref, request.confidence, request.verified_state)
        return {"id": memory_id}

    @app.delete("/v1/memory/{memory_id}")
    async def forget_memory(memory_id: str) -> dict:
        database.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))
        return {"forgotten": memory_id}

    @app.post("/v1/memory/{memory_id}/supersede")
    async def supersede_memory(memory_id: str, replacement_id: str) -> dict:
        database.execute("UPDATE memory_items SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (replacement_id, memory_id))
        return {"superseded": memory_id, "replacement": replacement_id}

    @app.post("/v1/tools/run")
    async def run_tool(request: ToolRunRequest) -> dict:
        try:
            tool = app.state.tools.authorize(request.tool, request.profile)
        except (LookupError, ToolAuthorizationError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        job_id = str(uuid.uuid4())
        database.execute("INSERT INTO jobs(id, user_id, session_id, kind, status, payload_json) VALUES (?, ?, ?, 'tool_execution', 'queued', ?)", (job_id, request.user_id, request.session_id, json.dumps({"tool": request.tool, "args": request.args})))
        database.execute("UPDATE jobs SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = ?", (job_id,))
        async def execute_tool() -> None:
            try:
                result = await app.state.tools.run(job_id, request.tool, request.args, request.profile)
                app.state.execution.record_audit(request.user_id, job_id, tool.name, result, request.args)
                database.execute("UPDATE jobs SET status = ?, result_json = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?", (result.status.lower(), json.dumps(result.as_dict()), job_id))
            except asyncio.CancelledError:
                database.execute("UPDATE jobs SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP WHERE id = ?", (job_id,))
            except Exception as exc:
                database.execute("UPDATE jobs SET status = 'failed', result_json = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?", (json.dumps({"error": type(exc).__name__, "message": str(exc)}), job_id))
            finally:
                app.state.execution.active.pop(job_id, None)
        task = asyncio.create_task(execute_tool())
        app.state.execution.active[job_id] = task
        return {"job_id": job_id, "tool": request.tool, "status": "running", "visible_detail": {"hidden": None, "summary": f"{tool.display_name} accepted", "commands_results": {"tool": tool.name, "driver": "local", "arguments": request.args}}.get(request.tool_details, f"{tool.display_name} accepted")}

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict:
        cancelled = app.state.execution.cancel(job_id)
        return {"job_id": job_id, "cancel_requested": cancelled}

    @app.get("/v1/activity")
    async def activity() -> list[dict]:
        return [dict(row) for row in database.fetch_all("SELECT * FROM execution_audit ORDER BY created_at DESC LIMIT 100")]

    @app.get("/v1/providers")
    async def list_providers() -> list[dict]:
        return providers.list_providers()

    @app.post("/v1/providers")
    async def create_provider(request: ProviderCreate) -> dict:
        try:
            provider_id = providers.create_provider(request.name, request.adapter_type, request.endpoint, request.config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(database.fetch_one("SELECT * FROM providers WHERE id = ?", (provider_id,)))

    @app.post("/v1/providers/{provider_id}/health")
    async def check_provider(provider_id: str) -> dict:
        result = await app.state.health.check_provider(provider_id)
        return {"provider_id": provider_id, "state": result.state, "latency_ms": result.latency_ms, "error": result.error}

    @app.delete("/v1/providers/{provider_id}")
    async def delete_provider(provider_id: str) -> dict:
        if database.fetch_one("SELECT id FROM providers WHERE id = ?", (provider_id,)) is None:
            raise HTTPException(status_code=404, detail="Provider not found")
        providers.delete_provider(provider_id)
        return {"deleted": provider_id}

    @app.post("/v1/providers/{provider_id}/discover")
    async def discover_provider(provider_id: str) -> dict:
        models = await providers.discover_models(provider_id)
        return {"provider_id": provider_id, "models": models}

    @app.get("/v1/models")
    async def list_models() -> list[dict]:
        return providers.list_models()

    @app.post("/v1/benchmark/{provider_id}/{model_name}")
    async def benchmark(provider_id: str, model_name: str) -> dict:
        return (await benchmark_candidate(providers, provider_id, model_name)).as_dict()

    @app.get("/v1/roles")
    async def list_roles() -> list[dict]:
        return [dict(row) for row in database.fetch_all("SELECT * FROM roles WHERE enabled = 1 ORDER BY name")]

    @app.post("/v1/roles")
    async def create_role(request: RoleCreate) -> dict:
        role_id = str(uuid.uuid4())
        database.execute("INSERT INTO roles(id, name, purpose, requirements_json) VALUES (?, ?, ?, ?)", (role_id, request.name, request.purpose, json.dumps(request.requirements)))
        return {"id": role_id, "name": request.name, "purpose": request.purpose}

    @app.get("/v1/routes")
    async def list_routes() -> list[dict]:
        rows = database.fetch_all("SELECT r.*, ro.name AS role_name FROM routes r JOIN roles ro ON ro.id = r.role_id ORDER BY ro.name, r.priority")
        return [dict(row) for row in rows]

    @app.post("/v1/routes")
    async def create_route(request: RouteCreate) -> dict:
        route_id = str(uuid.uuid4())
        database.execute("INSERT INTO routes(id, name, role_id, priority, policy_json) VALUES (?, ?, ?, ?, ?)", (route_id, request.name, request.role_id, request.priority, json.dumps({"strategy": request.strategy})))
        for target in request.targets:
            database.execute("INSERT INTO route_targets(route_id, provider_id, model_id, ordinal, enabled, conditions_json) VALUES (?, ?, ?, ?, ?, ?)", (route_id, target["provider_id"], target["model_id"], target.get("ordinal", 0), int(target.get("enabled", True)), json.dumps(target.get("conditions", {}))))
        return {"id": route_id, "name": request.name}

    @app.put("/v1/routes/{route_id}")
    async def update_route(route_id: str, request: RouteUpdate) -> dict:
        route = database.fetch_one("SELECT id FROM routes WHERE id = ?", (route_id,))
        if route is None:
            raise HTTPException(status_code=404, detail="Route not found")
        database.execute("UPDATE routes SET priority = ?, policy_json = ? WHERE id = ?", (request.priority, json.dumps({"strategy": request.strategy}), route_id))
        database.execute("DELETE FROM route_targets WHERE route_id = ?", (route_id,))
        for target in request.targets:
            database.execute("INSERT INTO route_targets(route_id, provider_id, model_id, ordinal, enabled, conditions_json) VALUES (?, ?, ?, ?, ?, ?)", (route_id, target["provider_id"], target["model_id"], target.get("ordinal", 0), int(target.get("enabled", True)), json.dumps(target.get("conditions", {}))))
        return {"id": route_id, "updated": True}

    @app.get("/v1/routes/{route_id}/eligibility")
    async def route_eligibility(route_id: str) -> dict:
        route = database.fetch_one("SELECT role_id, policy_json FROM routes WHERE id = ?", (route_id,))
        if route is None:
            raise HTTPException(status_code=404, detail="Route not found")
        policy = json.loads(route["policy_json"])
        from .routing import RoutingEngine
        candidates = RoutingEngine(database).eligible_routes(route["role_id"], policy.get("strategy", "priority"))
        return {"routes": [candidate.__dict__ for candidate in candidates], "warnings": RoutingEngine(database).warnings(candidates)}

    @app.post("/v1/demo/seed")
    async def seed_demo() -> dict:
        """Create isolated mock providers for development review only."""
        existing = database.fetch_one("SELECT COUNT(*) AS count FROM providers WHERE adapter_type = 'mock'")["count"]
        if existing:
            return {"status": "already_seeded"}
        first = providers.install_mock_provider("Demo Local Provider", ["demo-small"], delay_ms=1)
        second = providers.install_mock_provider("Demo Backup Provider", ["demo-small"], delay_ms=3)
        await providers.discover_models(first)
        await providers.discover_models(second)
        database.execute("UPDATE providers SET health_status = 'healthy' WHERE id IN (?, ?)", (first, second))
        first_model = database.fetch_one("SELECT id FROM models WHERE provider_id = ?", (first,))["id"]
        second_model = database.fetch_one("SELECT id FROM models WHERE provider_id = ?", (second,))["id"]
        route_id = str(uuid.uuid4())
        database.execute("INSERT INTO routes(id, name, role_id, priority, policy_json) VALUES (?, 'Demo Secretary', 'role-secretary', 10, ?)", (route_id, json.dumps({"strategy": "lowest_latency"})))
        database.execute("INSERT INTO route_targets(route_id, provider_id, model_id, ordinal) VALUES (?, ?, ?, 0)", (route_id, first, first_model))
        database.execute("INSERT INTO route_targets(route_id, provider_id, model_id, ordinal) VALUES (?, ?, ?, 1)", (route_id, second, second_model))
        return {"status": "seeded", "provider_ids": [first, second], "route_id": route_id}

    @app.post("/v1/demo/failure")
    async def demo_failure(request: DemoFailure) -> dict:
        adapter = providers.adapters.get(request.provider_id)
        if not hasattr(adapter, "fail"):
            raise HTTPException(status_code=400, detail="Provider is not an isolated mock")
        adapter.fail = request.fail
        result = None
        for _ in range(3 if request.fail else 2):
            result = await app.state.health.check_provider(request.provider_id)
        return {"provider_id": request.provider_id, "fail": request.fail, "state": result.state if result else None}

    @app.post("/v1/sessions")
    async def create_session(request: SessionCreate) -> dict:
        core.sessions.ensure_user(request.user_id, request.display_name)
        session_id = core.sessions.create_session(request.user_id, request.title)
        return {"session_id": session_id, "user_id": request.user_id}

    @app.post("/v1/sessions/{session_id}/messages")
    async def create_message(session_id: str, request: MessageCreate) -> dict:
        try:
            response = await core.handle_message(
                request.user_id,
                session_id,
                request.content,
                request.display_name,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return response.__dict__

    @app.post("/v1/jobs")
    async def create_job(request: JobCreate) -> dict:
        job_id = await jobs.submit(
            request.kind,
            request.payload,
            request.user_id,
            request.session_id,
        )
        return {"job_id": job_id, "status": "queued"}

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.get("/v1/jobs")
    async def list_jobs() -> list[dict]:
        return [dict(row) for row in database.fetch_all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100")]

    return app


app = create_app()
