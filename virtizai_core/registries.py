from __future__ import annotations

import json
import hashlib
import uuid
from dataclasses import dataclass

from .db import Database


class ContextBroker:
    """Returns only explicitly requested context; secretary context is empty by default."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def secretary_context(self, user_id: str, session_id: str) -> dict:
        return {"user_id": user_id, "session_id": session_id, "memory": [], "projects": []}


class HealthManager:
    def __init__(self, database: Database) -> None:
        self.database = database

    def set_provider_status(self, provider_id: str, status: str) -> None:
        self.database.execute(
            "UPDATE providers SET health_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, provider_id),
        )

    def set_model_status(self, model_id: str, status: str) -> None:
        self.database.execute(
            "UPDATE models SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, model_id),
        )


class ProjectRegistry:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, name: str, description: str | None = None, root_path: str | None = None) -> str:
        project_id = str(uuid.uuid4())
        self.database.execute(
            "INSERT INTO projects(id, name, description, root_path) VALUES (?, ?, ?, ?)",
            (project_id, name, description, root_path),
        )
        return project_id


class EnvironmentRegistry:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, name: str, target_type: str, address: str | None = None, credential_ref: str | None = None) -> str:
        target_id = str(uuid.uuid4())
        self.database.execute(
            """
            INSERT INTO environment_targets(id, name, target_type, address, credential_ref)
            VALUES (?, ?, ?, ?, ?)
            """,
            (target_id, name, target_type, address, credential_ref),
        )
        return target_id


class WorkerRegistry:
    """Registry for configured workers; it does not execute work."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self, name: str, worker_type: str, enabled: bool = True, status: str = "unknown",
        capabilities: list[str] | None = None, config: dict | None = None,
    ) -> str:
        worker_id = str(uuid.uuid4())
        self.database.execute(
            """
            INSERT INTO workers(id, name, worker_type, enabled, status, capabilities_json, config_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (worker_id, name, worker_type, int(enabled), status, json.dumps(capabilities or []), json.dumps(config or {})),
        )
        return worker_id


class ToolRegistry:
    def __init__(self, database: Database) -> None:
        self.database = database

    def register(self, name: str, description: str, input_schema: dict, policy: dict | None = None) -> str:
        tool_id = str(uuid.uuid4())
        self.database.execute(
            """
            INSERT INTO tools(id, name, description, input_schema_json, execution_policy_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tool_id, name, description, json.dumps(input_schema), json.dumps(policy or {})),
        )
        return tool_id


class IntegrationRegistry:
    def __init__(self, database: Database) -> None:
        self.database = database

    def register(self, name: str, integration_type: str, credential_ref: str | None = None) -> str:
        integration_id = str(uuid.uuid4())
        self.database.execute(
            """
            INSERT INTO integrations(id, name, integration_type, credential_ref)
            VALUES (?, ?, ?, ?)
            """,
            (integration_id, name, integration_type, credential_ref),
        )
        return integration_id


class MemoryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, content: str, namespace: str, user_id: str | None = None, project_id: str | None = None) -> str:
        memory_id = str(uuid.uuid4())
        self.database.execute(
            """
            INSERT INTO memory_items(id, user_id, project_id, namespace, content)
            VALUES (?, ?, ?, ?, ?)
            """,
            (memory_id, user_id, project_id, namespace, content),
        )
        return memory_id


class UpdateManager:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, version: str, action: str, status: str, release_ref: str | None = None) -> str:
        update_id = str(uuid.uuid4())
        self.database.execute(
            """
            INSERT INTO update_history(id, version, action, status, release_ref)
            VALUES (?, ?, ?, ?, ?)
            """,
            (update_id, version, action, status, release_ref),
        )
        return update_id

    def import_manifest(self, manifest: dict) -> dict:
        required = {"version", "channel", "release_url", "artifacts", "schema_compatibility", "rollback_compatibility"}
        missing = sorted(required - manifest.keys())
        if missing:
            raise ValueError(f"Release manifest is missing: {', '.join(missing)}")
        if manifest["channel"] not in {"stable", "beta", "nightly"}:
            raise ValueError("Unsupported release channel")
        if not isinstance(manifest["artifacts"], list) or not manifest["artifacts"]:
            raise ValueError("Release manifest requires at least one artifact")
        for artifact in manifest["artifacts"]:
            if not {"platform", "url", "sha256"} <= artifact.keys() or len(artifact["sha256"]) != 64:
                raise ValueError("Each artifact requires platform, url, and SHA-256")
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        self.database.execute(
            """INSERT INTO release_manifests(version, channel, release_url, manifest_json, manifest_sha256, published_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET channel=excluded.channel, release_url=excluded.release_url,
                 manifest_json=excluded.manifest_json, manifest_sha256=excluded.manifest_sha256, published_at=excluded.published_at""",
            (manifest["version"], manifest["channel"], manifest["release_url"], canonical, digest, manifest.get("published_at")),
        )
        return {"version": manifest["version"], "manifest_sha256": digest}

    def releases(self) -> list[dict]:
        rows = self.database.fetch_all("SELECT * FROM release_manifests ORDER BY version DESC")
        return [{**dict(row), "manifest": json.loads(row["manifest_json"])} for row in rows]

    def policy(self) -> dict:
        row = dict(self.database.fetch_one("SELECT * FROM update_policies WHERE id = 'default'"))
        row["skipped_versions"] = json.loads(row.pop("skipped_versions_json"))
        return row

    def set_policy(self, channel: str, version_policy: str, pinned_version: str | None, skipped_versions: list[str]) -> dict:
        if channel not in {"stable", "beta", "nightly"}:
            raise ValueError("Unsupported release channel")
        if version_policy not in {"follow_channel", "stay_minor", "pin_exact"}:
            raise ValueError("Unsupported version policy")
        if version_policy == "pin_exact" and not pinned_version:
            raise ValueError("A pinned version is required")
        self.database.execute(
            """UPDATE update_policies SET channel=?, version_policy=?, pinned_version=?, skipped_versions_json=?, updated_at=CURRENT_TIMESTAMP
               WHERE id='default'""",
            (channel, version_policy, pinned_version, json.dumps(sorted(set(skipped_versions)))),
        )
        return self.policy()

    def plan(self, current_version: str, platform: str) -> dict:
        policy = self.policy()
        releases = self.releases()
        candidates = [item for item in releases if item["channel"] == policy["channel"] and item["version"] not in policy["skipped_versions"]]
        if policy["version_policy"] == "pin_exact":
            candidates = [item for item in candidates if item["version"] == policy["pinned_version"]]
        elif policy["version_policy"] == "stay_minor":
            prefix = ".".join(current_version.split(".")[:2]) + "."
            candidates = [item for item in candidates if item["version"].startswith(prefix)]
        for release in candidates:
            artifact = next((item for item in release["manifest"]["artifacts"] if item["platform"] == platform), None)
            if artifact:
                return {"available": release["version"] != current_version, "current_version": current_version, "release": release["manifest"], "artifact": artifact, "policy": policy}
        return {"available": False, "current_version": current_version, "policy": policy}

    def history(self) -> list[dict]:
        return [dict(row) for row in self.database.fetch_all("SELECT * FROM update_history ORDER BY created_at DESC LIMIT 100")]
