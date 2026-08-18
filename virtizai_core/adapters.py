from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


PROVIDER_STATES = {"healthy", "degraded", "unavailable", "disabled", "unknown"}
MODEL_STATES = {"warm", "cold", "loading", "available", "error", "not_installed", "unknown"}


@dataclass(frozen=True)
class ProviderHealth:
    state: str
    latency_ms: float | None = None
    error: str | None = None
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveredModel:
    name: str
    display_name: str | None = None
    context_limit: int | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    state: str = "available"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InferenceResponse:
    content: str
    model_name: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    ttft_ms: float | None
    latency_ms: float
    usage_exact: bool
    estimated_cost: float | None = None


class ProviderAdapter(Protocol):
    async def health(self) -> ProviderHealth: ...
    async def list_models(self) -> list[DiscoveredModel]: ...
    async def get_model_metadata(self, model_name: str) -> DiscoveredModel: ...
    async def chat(self, messages: list[dict[str, str]], model_name: str, max_tokens: int | None = None) -> InferenceResponse: ...


class AdapterError(RuntimeError):
    pass


class OllamaAdapter:
    """Generic Ollama adapter. Endpoint and model names are user configuration."""

    capabilities = ("health", "list_models", "model_metadata", "chat")

    def __init__(self, endpoint: str, timeout_seconds: float = 20.0) -> None:
        normalized = endpoint.rstrip("/")
        if normalized.endswith("/v1"):
            normalized = normalized[:-3]
        self.endpoint = normalized
        self.timeout_seconds = timeout_seconds

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AdapterError(str(exc)) from exc

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            await asyncio.to_thread(self._request, "/api/tags")
            return ProviderHealth("healthy", (time.perf_counter() - started) * 1000, capabilities=self.capabilities)
        except AdapterError as exc:
            return ProviderHealth("unavailable", (time.perf_counter() - started) * 1000, str(exc), self.capabilities)

    async def list_models(self) -> list[DiscoveredModel]:
        payload = await asyncio.to_thread(self._request, "/api/tags")
        return [
            DiscoveredModel(
                name=item["name"],
                display_name=item.get("name"),
                state="available",
                metadata={"size": item.get("size"), "modified_at": item.get("modified_at")},
            )
            for item in payload.get("models", [])
        ]

    async def get_model_metadata(self, model_name: str) -> DiscoveredModel:
        payload = await asyncio.to_thread(self._request, "/api/show", {"name": model_name})
        model_info = payload.get("model_info", {})
        capabilities = payload.get("capabilities", [])
        return DiscoveredModel(
            name=model_name,
            display_name=model_name,
            context_limit=model_info.get("llama.context_length"),
            capabilities={capability: True for capability in capabilities},
            state="available",
            metadata={"details": payload.get("details", {})},
        )

    async def chat(self, messages: list[dict[str, str]], model_name: str, max_tokens: int | None = None) -> InferenceResponse:
        started = time.perf_counter()
        payload = await asyncio.to_thread(
            self._request,
            "/api/chat",
            {"model": model_name, "messages": messages, "stream": False, **({"options": {"num_predict": max_tokens}} if max_tokens else {})},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        message = payload.get("message", {})
        prompt_tokens = payload.get("prompt_eval_count")
        output_tokens = payload.get("eval_count")
        return InferenceResponse(
            content=message.get("content", ""),
            model_name=payload.get("model", model_name),
            input_tokens=prompt_tokens,
            output_tokens=output_tokens,
            total_tokens=(prompt_tokens + output_tokens) if prompt_tokens is not None and output_tokens is not None else None,
            ttft_ms=None,
            latency_ms=latency_ms,
            usage_exact=prompt_tokens is not None or output_tokens is not None,
        )


class MockProviderAdapter:
    """Deterministic isolated adapter for tests and development demonstrations."""

    def __init__(self, models: list[str], response_prefix: str = "Mock response", fail: bool = False, delay_ms: float = 0) -> None:
        self.models = models
        self.response_prefix = response_prefix
        self.fail = fail
        self.delay_ms = delay_ms
        self.health_calls = 0

    async def health(self) -> ProviderHealth:
        self.health_calls += 1
        if self.fail:
            return ProviderHealth("unavailable", error="isolated mock failure", capabilities=("health", "list_models", "chat"))
        return ProviderHealth("healthy", 0.1, capabilities=("health", "list_models", "chat"))

    async def list_models(self) -> list[DiscoveredModel]:
        if self.fail:
            raise AdapterError("isolated mock discovery failure")
        return [DiscoveredModel(name=model, state="available", capabilities={"reasoning": 0.6, "coding": 0.6}) for model in self.models]

    async def get_model_metadata(self, model_name: str) -> DiscoveredModel:
        models = await self.list_models()
        for model in models:
            if model.name == model_name:
                return model
        raise AdapterError(f"model not found: {model_name}")

    async def chat(self, messages: list[dict[str, str]], model_name: str, max_tokens: int | None = None) -> InferenceResponse:
        if self.fail:
            raise AdapterError("isolated mock inference failure")
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        content = f"{self.response_prefix} from {model_name}"
        return InferenceResponse(content, model_name, 3, 5, 8, 0.1, max(self.delay_ms, 0.1), True)
