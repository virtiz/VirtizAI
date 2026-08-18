from __future__ import annotations

import json
import uuid
import time
from dataclasses import dataclass

from .db import Database


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class ContextBundle:
    budget_name: str
    max_tokens: int
    used_tokens: int
    memory: list[dict]
    projects: list[dict]
    environments: list[dict]
    conversation: list[dict]
    tools: list[dict]
    category_tokens: dict[str, int]
    omitted_sources: list[str]
    build_id: str

    def as_dict(self) -> dict:
        return {
            "budget_name": self.budget_name,
            "max_tokens": self.max_tokens,
            "used_tokens": self.used_tokens,
            "memory": self.memory,
            "projects": self.projects,
            "environments": self.environments,
            "conversation": self.conversation,
            "tools": self.tools,
            "category_tokens": self.category_tokens,
            "omitted_sources": self.omitted_sources,
            "build_id": self.build_id,
        }


class ContextBroker:
    """On-demand scoped retrieval with explicit budgets; no global hydration."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def budget(self, name: str = "secretary") -> dict:
        row = self.database.fetch_one("SELECT * FROM context_budgets WHERE name = ?", (name,))
        if row is None:
            row = self.database.fetch_one("SELECT * FROM context_budgets WHERE name = 'secretary'")
        return dict(row)

    def retrieve(self, user_id: str, session_id: str, project_id: str | None = None, query: str | None = None, budget_name: str = "secretary", requested_sources: list[str] | None = None, requested_tools: list[dict] | None = None) -> ContextBundle:
        started = time.perf_counter()
        limits = self.budget(budget_name)
        memory_limit = limits["memory_tokens"]
        requested_sources = requested_sources or ["conversation", "memory", "project", "environment"]
        memory_rows = self.database.fetch_all(
            "SELECT * FROM memory_items WHERE (user_id = ? OR user_id IS NULL) AND (? IS NULL OR project_id = ?) AND memory_type = 'durable' AND superseded_by IS NULL ORDER BY importance DESC, updated_at DESC",
            (user_id, project_id, project_id),
        )
        memory: list[dict] = []
        used = 0
        for row in memory_rows:
            item = dict(row)
            if query and query.lower() not in item["content"].lower():
                continue
            tokens = item.get("token_estimate") or estimate_tokens(item["content"])
            if used + tokens > memory_limit:
                continue
            memory.append({"id": item["id"], "namespace": item["namespace"], "content": item["content"], "source_ref": item["source_ref"], "tokens": tokens})
            used += tokens
        projects: list[dict] = []
        if project_id and "project" in requested_sources:
            project = self.database.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
            if project:
                projects.append(dict(project))
        environments = []
        if project_id and "environment" in requested_sources:
            environments = [dict(row) for row in self.database.fetch_all(
                "SELECT e.* FROM environment_targets e JOIN project_environment_targets pe ON pe.environment_target_id = e.id WHERE pe.project_id = ?",
                (project_id,),
            )]
        conversation = []
        if "conversation" in requested_sources:
            conversation = [dict(row) for row in self.database.fetch_all("SELECT id, role, content, created_at FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT 6", (session_id,))][::-1]
        tools = requested_tools or []
        omitted: list[str] = []
        # Keep the returned package itself within the aggregate budget, oldest/lowest
        # priority material is omitted before token accounting is persisted.
        categories = [("conversation", conversation), ("memory", memory), ("project", projects), ("environment", environments), ("tools", tools)]
        remaining = limits["max_tokens"]
        for category, items in categories:
            while items and remaining < sum(estimate_tokens(json.dumps(item)) for item in items):
                items.pop(0)
                omitted.append(category)
            remaining -= sum(estimate_tokens(json.dumps(item)) for item in items)
        used = sum(item["tokens"] for item in memory)
        category_tokens = {"conversation": sum(estimate_tokens(item["content"]) for item in conversation), "memory": used, "project": sum(estimate_tokens(json.dumps(item)) for item in projects), "environment": sum(estimate_tokens(json.dumps(item)) for item in environments), "tools": sum(estimate_tokens(json.dumps(item)) for item in tools)}
        total = sum(category_tokens.values())
        omitted.extend(source for source in ("conversation", "memory", "project", "environment", "tools") if source not in requested_sources or category_tokens.get(source, 0) == 0)
        build_id = str(uuid.uuid4())
        self.database.execute("INSERT INTO context_builds(id, user_id, session_id, project_id, budget_name, category_tokens_json, selected_sources_json, omitted_sources_json, total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (build_id, user_id, session_id, project_id, budget_name, json.dumps(category_tokens), json.dumps(requested_sources), json.dumps(omitted), total))
        return ContextBundle(budget_name, limits["max_tokens"], total, memory, projects, environments, conversation, tools, category_tokens, omitted, build_id)

    def secretary_context(self, user_id: str, session_id: str) -> dict:
        return self.retrieve(user_id, session_id).as_dict()


class MemoryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, content: str, namespace: str, user_id: str | None = None, project_id: str | None = None, memory_type: str = "durable", importance: float = 0.5, source_ref: str | None = None, confidence: float = 0.5, verified_state: str = "unverified") -> str:
        memory_id = str(uuid.uuid4())
        if user_id:
            self.database.execute("INSERT INTO users(id, display_name) VALUES (?, ?) ON CONFLICT(id) DO NOTHING", (user_id, user_id))
        self.database.execute(
            "INSERT INTO memory_items(id, user_id, project_id, namespace, content, memory_type, importance, source_ref, token_estimate, confidence, verified_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, user_id, project_id, namespace, content, memory_type, importance, source_ref, estimate_tokens(content), confidence, verified_state),
        )
        return memory_id

    def list(self, user_id: str | None = None, project_id: str | None = None) -> list[dict]:
        return [dict(row) for row in self.database.fetch_all("SELECT * FROM memory_items WHERE (? IS NULL OR user_id = ?) AND (? IS NULL OR project_id = ?) ORDER BY updated_at DESC", (user_id, user_id, project_id, project_id))]
