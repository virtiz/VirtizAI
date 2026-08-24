from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .adapters import DiscoveredModel, InferenceResponse, MockProviderAdapter, OllamaAdapter, OpenAICompatibleAdapter, ProviderAdapter
from .db import Database


class ProviderRegistry:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.adapters: dict[str, ProviderAdapter] = {}

    def register_adapter(self, provider_id: str, adapter: ProviderAdapter) -> None:
        self.adapters[provider_id] = adapter

    def restore_adapters(self) -> None:
        """Rehydrate configured adapter types without copying provider secrets."""
        for provider in self.database.fetch_all("SELECT * FROM providers"):
            if provider["adapter_type"] == "ollama":
                config = json.loads(provider["config_json"] or "{}")
                self.register_adapter(provider["id"], self._adapter("ollama", provider["endpoint"], config))
            elif provider["adapter_type"] == "openai_compatible":
                config = json.loads(provider["config_json"] or "{}")
                self.register_adapter(provider["id"], self._adapter("openai_compatible", provider["endpoint"], config))
            elif provider["adapter_type"] == "mock":
                models = [row["name"] for row in self.database.fetch_all("SELECT name FROM models WHERE provider_id = ?", (provider["id"],))]
                self.register_adapter(provider["id"], MockProviderAdapter(models, response_prefix=provider["name"]))

    def create_provider(self, name: str, adapter_type: str, endpoint: str | None, config: dict[str, Any] | None = None, adapter: ProviderAdapter | None = None) -> str:
        existing = self.database.fetch_one(
            "SELECT id FROM providers WHERE name = ? AND adapter_type = ? AND COALESCE(endpoint, '') = COALESCE(?, '')",
            (name.strip(), adapter_type, endpoint),
        )
        if existing:
            provider_id = existing["id"]
            if provider_id not in self.adapters:
                self.restore_adapters()
            return provider_id
        provider_id = str(uuid.uuid4())
        self.database.execute(
            "INSERT INTO providers(id, name, adapter_type, endpoint, config_json) VALUES (?, ?, ?, ?, ?)",
            (provider_id, name, adapter_type, endpoint, json.dumps(config or {})),
        )
        self.register_adapter(provider_id, adapter or self._adapter(adapter_type, endpoint, config or {}))
        return provider_id

    def delete_provider(self, provider_id: str) -> None:
        self.database.execute("DELETE FROM route_targets WHERE provider_id = ?", (provider_id,))
        self.database.execute("DELETE FROM models WHERE provider_id = ?", (provider_id,))
        self.database.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        self.adapters.pop(provider_id, None)

    def _adapter(self, adapter_type: str, endpoint: str | None, config: dict[str, Any]) -> ProviderAdapter:
        if adapter_type == "ollama":
            if not endpoint:
                raise ValueError("Ollama providers require an endpoint")
            return OllamaAdapter(endpoint, float(config.get("timeout_seconds", 20)), config.get("chat_options"))
        if adapter_type == "openai_compatible":
            base_url = config.get("base_url")
            if not isinstance(base_url, str) or not base_url.strip(): raise ValueError("OpenAI-compatible providers require config.base_url")
            api_key = config.get("api_key")
            if api_key is not None and not isinstance(api_key, str): raise ValueError("OpenAI-compatible config.api_key must be a string")
            return OpenAICompatibleAdapter(base_url, float(config.get("timeout_seconds", 20)), api_key, config.get("chat_options"))
        raise ValueError(f"Unsupported adapter type: {adapter_type}")

    def adapter_for(self, provider_id: str) -> ProviderAdapter:
        adapter = self.adapters.get(provider_id)
        if adapter is None:
            raise LookupError(f"No adapter registered for provider: {provider_id}")
        return adapter

    async def discover_models(self, provider_id: str) -> list[str]:
        discovered = await self.adapter_for(provider_id).list_models()
        for model in discovered:
            self._upsert_model(provider_id, model)
        return [model.name for model in discovered]

    async def health(self, provider_id: str):
        return await self.adapter_for(provider_id).health()

    async def chat(self, provider_id: str, model_name: str, messages: list[dict[str, str]], max_tokens: int | None = None, tools: list[dict[str, Any]] | None = None, tool_choice: Any = None) -> InferenceResponse:
        row = self.database.fetch_one("SELECT user_overrides_json FROM models WHERE provider_id=? AND name=?", (provider_id, model_name))
        overrides = json.loads((row["user_overrides_json"] if row else "{}") or "{}")
        return await self.adapter_for(provider_id).chat(messages, model_name, max_tokens, overrides.get("keep_alive"), tools, tool_choice)

    async def prewarm(self, provider_id: str, model_name: str) -> float:
        row = self.database.fetch_one("SELECT user_overrides_json FROM models WHERE provider_id=? AND name=?", (provider_id, model_name))
        overrides = json.loads((row["user_overrides_json"] if row else "{}") or "{}")
        adapter = self.adapter_for(provider_id)
        if not hasattr(adapter, "prewarm"):
            return 0.0
        latency = await adapter.prewarm(model_name, overrides.get("keep_alive"))
        overrides.update({"residency": "warm", "last_warmup": datetime.now(timezone.utc).isoformat()})
        model_id = self.database.fetch_one("SELECT id FROM models WHERE provider_id=? AND name=?", (provider_id, model_name))["id"]
        self.database.execute("UPDATE models SET status='warm', user_overrides_json=?, last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(overrides), model_id))
        return latency

    async def residency(self, provider_id: str) -> set[str]:
        adapter = self.adapter_for(provider_id)
        if not hasattr(adapter, "residency"):
            return set()
        return await adapter.residency()

    def _upsert_model(self, provider_id: str, model: DiscoveredModel) -> str:
        row = self.database.fetch_one("SELECT id FROM models WHERE provider_id = ? AND name = ?", (provider_id, model.name))
        model_id = row["id"] if row else str(uuid.uuid4())
        values = (model.state, model.context_limit, json.dumps(model.capabilities), json.dumps(model.metadata), model_id)
        if row:
            self.database.execute(
                "UPDATE models SET status = ?, context_window = ?, capabilities_json = ?, metadata_json = ?, last_seen_at = CURRENT_TIMESTAMP, last_error = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values,
            )
        else:
            self.database.execute(
                "INSERT INTO models(id, provider_id, name, capabilities_json, status, context_window, metadata_json, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (model_id, provider_id, model.name, json.dumps(model.capabilities), model.state, model.context_limit, json.dumps(model.metadata)),
            )
        return model_id

    def set_model_override(self, model_id: str, overrides: dict[str, Any]) -> None:
        self.database.execute("UPDATE models SET user_overrides_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (json.dumps(overrides), model_id))

    def list_providers(self) -> list[dict]:
        return [self._public_provider(dict(row)) for row in self.database.fetch_all("SELECT * FROM providers ORDER BY name")]

    @staticmethod
    def _public_provider(provider: dict) -> dict:
        config = json.loads(provider.get("config_json") or "{}")
        if isinstance(config, dict):
            for key in ("api_key", "authorization", "token", "password"): config.pop(key, None)
        provider["config_json"] = json.dumps(config)
        return provider

    def list_models(self) -> list[dict]:
        return [dict(row) for row in self.database.fetch_all("SELECT m.*, p.name AS provider_name FROM models m JOIN providers p ON p.id = m.provider_id ORDER BY p.name, m.name")]

    def install_mock_provider(self, name: str, models: list[str], fail: bool = False, delay_ms: float = 0) -> str:
        return self.create_provider(name, "mock", None, adapter=MockProviderAdapter(models, response_prefix=name, fail=fail, delay_ms=delay_ms))
