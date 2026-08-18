from __future__ import annotations

import json
import uuid

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
