from virtizai_core.adapters import OllamaAdapter


def test_ollama_endpoint_normalizes_openai_v1_suffix() -> None:
    assert OllamaAdapter("http://example.test:11434/v1").endpoint == "http://example.test:11434"
    assert OllamaAdapter("http://example.test:11434/").endpoint == "http://example.test:11434"



def test_ollama_chat_options_are_preserved() -> None:
    adapter = OllamaAdapter("http://example.test:11434", chat_options={"num_ctx": 2048})
    assert adapter.chat_options == {"num_ctx": 2048}
