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
    tool_calls: tuple[dict[str, Any], ...] = ()


class ProviderAdapter(Protocol):
    async def health(self) -> ProviderHealth: ...
    async def list_models(self) -> list[DiscoveredModel]: ...
    async def get_model_metadata(self, model_name: str) -> DiscoveredModel: ...
    async def chat(self, messages: list[dict[str, str]], model_name: str, max_tokens: int | None = None, keep_alive: int | str | None = None, tools: list[dict[str, Any]] | None = None, tool_choice: Any = None) -> InferenceResponse: ...
    async def prewarm(self, model_name: str, keep_alive: int | str | None = None) -> float: ...
    async def residency(self) -> set[str]: ...


class AdapterError(RuntimeError):
    pass


class OllamaAdapter:
    """Generic Ollama adapter. Endpoint and model names are user configuration."""

    capabilities = ("health", "list_models", "model_metadata", "chat")

    def __init__(self, endpoint: str, timeout_seconds: float = 20.0, chat_options: dict[str, Any] | None = None) -> None:
        normalized = endpoint.rstrip("/")
        if normalized.endswith("/v1"):
            normalized = normalized[:-3]
        self.endpoint = normalized
        self.timeout_seconds = timeout_seconds
        self.chat_options = dict(chat_options or {})

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
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode(errors="replace")
            except Exception:
                detail = str(exc)
            raise AdapterError(detail[:500]) from exc
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

    async def chat(self, messages: list[dict[str, str]], model_name: str, max_tokens: int | None = None, keep_alive: int | str | None = None, tools: list[dict[str, Any]] | None = None, tool_choice: Any = None) -> InferenceResponse:
        if tools is not None:
            raise AdapterError('Provider does not support native tool calling')
        started = time.perf_counter()
        options = dict(self.chat_options)
        if max_tokens:
            options["num_predict"] = max_tokens
        payload = await asyncio.to_thread(
            self._request,
            "/api/chat",
            {"model": model_name, "messages": messages, "stream": False, **({"options": options} if options else {}), **({"keep_alive": keep_alive} if keep_alive is not None else {})},
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

    async def prewarm(self, model_name: str, keep_alive: int | str | None = None) -> float:
        started = time.perf_counter()
        await asyncio.to_thread(self._request, "/api/chat", {
            "model": model_name,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "stream": False,
            "options": {"num_predict": 1, **self.chat_options},
            **({"keep_alive": keep_alive} if keep_alive is not None else {}),
        })
        return (time.perf_counter() - started) * 1000

    async def residency(self) -> set[str]:
        payload = await asyncio.to_thread(self._request, "/api/ps")
        return {str(item.get("name") or item.get("model")) for item in payload.get("models", [])}


class OpenAICompatibleAdapter:
    """Generic adapter for OpenAI chat-completions-compatible services."""
    capabilities = ("health", "list_models", "model_metadata", "chat")
    def __init__(self, base_url: str, timeout_seconds: float = 20.0, api_key: str | None = None, chat_options: dict[str, Any] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v1"): self.base_url = self.base_url[:-3]
        if not self.base_url: raise ValueError("OpenAI-compatible providers require config.base_url")
        self.timeout_seconds, self.api_key = timeout_seconds, api_key
        self.chat_options = dict(chat_options or {})
    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key: headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response: result = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc: raise AdapterError(f"OpenAI-compatible request failed with HTTP status {exc.code}") from exc
        except urllib.error.URLError as exc: raise AdapterError("OpenAI-compatible connection failed") from exc
        except (TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc: raise AdapterError("OpenAI-compatible response was malformed or timed out") from exc
        if not isinstance(result, dict): raise AdapterError("OpenAI-compatible response must be a JSON object")
        return result
    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            await asyncio.to_thread(self._request, "/v1/models")
            return ProviderHealth("healthy", (time.perf_counter() - started) * 1000, capabilities=self.capabilities)
        except AdapterError as exc: return ProviderHealth("unavailable", (time.perf_counter() - started) * 1000, str(exc), self.capabilities)
    async def list_models(self) -> list[DiscoveredModel]:
        payload = await asyncio.to_thread(self._request, "/v1/models")
        data = payload.get("data")
        if not isinstance(data, list): raise AdapterError("OpenAI-compatible models response is missing data")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] for item in data): raise AdapterError("OpenAI-compatible models response has an invalid model")
        return [DiscoveredModel(name=item["id"], display_name=item["id"], metadata={key: value for key, value in item.items() if key != "id"}) for item in data]
    async def get_model_metadata(self, model_name: str) -> DiscoveredModel:
        for model in await self.list_models():
            if model.name == model_name: return model
        raise AdapterError(f"model not found: {model_name}")
    async def chat(self, messages: list[dict[str, str]], model_name: str, max_tokens: int | None = None, keep_alive: int | str | None = None, tools: list[dict[str, Any]] | None = None, tool_choice: Any = None) -> InferenceResponse:
        started = time.perf_counter()
        request: dict[str, Any] = {"model": model_name, "messages": messages, "stream": False, **self.chat_options}
        if max_tokens is not None: request["max_tokens"] = max_tokens
        if tools is not None: request["tools"] = tools
        if tool_choice is not None: request["tool_choice"] = tool_choice
        response = await asyncio.to_thread(self._request, "/v1/chat/completions", request)
        choices = response.get("choices")
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(message, dict) or (not isinstance(message.get("content"), str) and not isinstance(tool_calls, list)):
            raise AdapterError("OpenAI-compatible chat response is missing assistant content or tool calls")
        if tool_calls is not None and (not isinstance(tool_calls, list) or any(not isinstance(call, dict) for call in tool_calls)):
            raise AdapterError("OpenAI-compatible chat response has invalid tool calls")
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        input_tokens = usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None
        output_tokens = usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None
        total_tokens = usage.get("total_tokens") if isinstance(usage.get("total_tokens"), int) else (input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None)
        return InferenceResponse(message.get("content") if isinstance(message.get("content"), str) else "", response.get("model") if isinstance(response.get("model"), str) else model_name, input_tokens, output_tokens, total_tokens, None, (time.perf_counter() - started) * 1000, bool(usage), tool_calls=tuple(tool_calls or ()))


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

    async def chat(self, messages: list[dict[str, str]], model_name: str, max_tokens: int | None = None, keep_alive: int | str | None = None, tools: list[dict[str, Any]] | None = None, tool_choice: Any = None) -> InferenceResponse:
        if self.fail:
            raise AdapterError("isolated mock inference failure")
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        content = f"{self.response_prefix} from {model_name}"
        return InferenceResponse(content, model_name, 3, 5, 8, 0.1, max(self.delay_ms, 0.1), True)
