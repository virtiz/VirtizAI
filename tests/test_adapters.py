from virtizai_core.adapters import OllamaAdapter


def test_ollama_endpoint_normalizes_openai_v1_suffix() -> None:
    assert OllamaAdapter("http://example.test:11434/v1").endpoint == "http://example.test:11434"
    assert OllamaAdapter("http://example.test:11434/").endpoint == "http://example.test:11434"
