from virtizai_core.adapters import OllamaAdapter
import pytest


def test_ollama_endpoint_normalizes_openai_v1_suffix() -> None:
    assert OllamaAdapter("http://example.test:11434/v1").endpoint == "http://example.test:11434"
    assert OllamaAdapter("http://example.test:11434/").endpoint == "http://example.test:11434"



def test_ollama_chat_options_are_preserved() -> None:
    adapter = OllamaAdapter("http://example.test:11434", chat_options={"num_ctx": 2048})
    assert adapter.chat_options == {"num_ctx": 2048}


@pytest.mark.asyncio
async def test_ollama_model_keep_alive_and_residency_are_supported() -> None:
    adapter = OllamaAdapter("http://example.test:11434", chat_options={"num_ctx": 2048})
    calls = []

    def request(path, payload=None):
        calls.append((path, payload))
        if path == "/api/ps":
            return {"models": [{"name": "phi4-mini:latest"}]}
        if payload and payload.get("messages", [{}])[0].get("content") == "Reply with OK.":
            return {"message": {"content": "OK"}}
        return {"model": "phi4-mini:latest", "message": {"content": "hello"}, "prompt_eval_count": 1, "eval_count": 1}

    adapter._request = request
    await adapter.prewarm("phi4-mini:latest", -1)
    assert calls[0][0] == "/api/chat"
    assert calls[0][1]["keep_alive"] == -1
    assert await adapter.residency() == {"phi4-mini:latest"}
