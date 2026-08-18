from pathlib import Path

from virtizai_core.db import Database
from virtizai_core.providers import ProviderRegistry


def test_identical_provider_configuration_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    registry = ProviderRegistry(database)
    first = registry.create_provider("Local", "ollama", "http://example.test:11434", adapter=object())
    second = registry.create_provider(" Local ", "ollama", "http://example.test:11434", adapter=object())
    assert first == second
    assert database.fetch_one("SELECT COUNT(*) FROM providers")[0] == 1
    database.close()
