from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from virtizai_core.adapters import AdapterError, OllamaAdapter, OpenAICompatibleAdapter
from virtizai_core.db import Database
from virtizai_core.providers import ProviderRegistry


@pytest.fixture
def server():
    requests, responses = [], {
        ("GET", "/v1/models"): (200, {"data": [{"id": "model-a", "owned_by": "test"}]}),
        ("POST", "/v1/chat/completions"): (200, {"model": "model-a", "choices": [{"message": {"role": "assistant", "content": "answer"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}),
    }
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self): self.respond()
        def do_POST(self): self.respond()
        def respond(self):
            length = int(self.headers.get("Content-Length", "0"))
            requests.append((self.command, self.path, dict(self.headers), json.loads(self.rfile.read(length)) if length else None))
            status, body = responses.get((self.command, self.path), (404, {"error": "missing"}))
            encoded = json.dumps(body).encode()
            self.send_response(status); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
        def log_message(self, *_): pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
    try: yield f"http://127.0.0.1:{httpd.server_port}", requests, responses
    finally: httpd.shutdown(); thread.join()


@pytest.mark.asyncio
async def test_openai_compatible_health_discovery_and_chat(server):
    base_url, requests, _ = server
    adapter = OpenAICompatibleAdapter(f"{base_url}/v1", api_key="test-secret")
    assert (await adapter.health()).state == "healthy"
    assert [model.name for model in await adapter.list_models()] == ["model-a"]
    result = await adapter.chat([{"role": "system", "content": "be helpful"}, {"role": "user", "content": "hello"}], "model-a", max_tokens=7)
    assert result.content == "answer" and result.total_tokens == 5
    assert [(method, path) for method, path, _, _ in requests] == [("GET", "/v1/models"), ("GET", "/v1/models"), ("POST", "/v1/chat/completions")]
    assert requests[-1][3] == {"model": "model-a", "messages": [{"role": "system", "content": "be helpful"}, {"role": "user", "content": "hello"}], "stream": False, "max_tokens": 7}
    assert requests[-1][2]["Authorization"] == "Bearer test-secret"


@pytest.mark.asyncio
async def test_openai_compatible_native_tools_preserve_configured_chat_options(server):
    base_url, requests, responses = server
    responses[("POST", "/v1/chat/completions")] = (200, {"model": "model-a", "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "inspect_file", "arguments": "{\"path\": \"README.md\"}"}}]}}]})
    adapter = OpenAICompatibleAdapter(base_url, chat_options={"thinking_budget": 0})
    tools = [{"type": "function", "function": {"name": "inspect_file", "parameters": {"type": "object"}}}]
    result = await adapter.chat([{"role": "user", "content": "inspect"}], "model-a", tools=tools, tool_choice="auto")
    assert result.content == "" and result.tool_calls[0]["function"]["name"] == "inspect_file"
    request = requests[-1][3]
    assert request["thinking_budget"] == 0 and request["tools"] == tools and request["tool_choice"] == "auto"
    assert request["parallel_tool_calls"] is False


@pytest.mark.asyncio
async def test_persistence_restoration_and_provider_redaction(tmp_path: Path, server):
    base_url, _, _ = server
    database = Database(tmp_path / "state.db"); database.open()
    registry = ProviderRegistry(database)
    provider_id = registry.create_provider("Generic", "openai_compatible", "ignored-endpoint", {"base_url": base_url, "api_key": "test-secret"})
    assert await registry.discover_models(provider_id) == ["model-a"]
    assert database.fetch_one("SELECT name FROM models WHERE provider_id=?", (provider_id,))["name"] == "model-a"
    assert "test-secret" not in registry.list_providers()[0]["config_json"]
    restored = ProviderRegistry(database); restored.restore_adapters()
    assert (await restored.chat(provider_id, "model-a", [{"role": "user", "content": "hello"}])).content == "answer"
    database.close()


@pytest.mark.asyncio
async def test_failures_are_safe_and_existing_adapters_remain_usable(server):
    base_url, _, responses = server
    adapter = OpenAICompatibleAdapter(base_url, api_key="test-secret")
    responses[("GET", "/v1/models")] = (401, {"error": "test-secret"})
    health = await adapter.health()
    assert health.state == "unavailable" and "test-secret" not in (health.error or "")
    responses[("POST", "/v1/chat/completions")] = (200, {"choices": [{}]})
    with pytest.raises(AdapterError, match="missing assistant content"): await adapter.chat([], "model-a")
    assert "test-secret" not in ((await OpenAICompatibleAdapter("http://127.0.0.1:1", 0.01, "test-secret").health()).error or "")
    ollama = OllamaAdapter("http://example.test:11434/v1")
    assert ollama.endpoint == "http://example.test:11434"
    database = Database(Path("/tmp") / "packet2-mock-test.db"); database.open()
    registry = ProviderRegistry(database); provider_id = registry.install_mock_provider("Mock", ["m"])
    assert (await registry.chat(provider_id, "m", [{"role": "user", "content": "x"}])).content.startswith("Mock")
    database.close()
