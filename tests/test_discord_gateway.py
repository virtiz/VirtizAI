import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.discord_gateway import DiscordGateway
from virtizai_core.secrets import FileSecretStore


def test_file_secret_store_is_persistent_and_never_reads_value_back_from_api(tmp_path: Path):
    path = tmp_path / "secrets.json"
    store = FileSecretStore(path)
    store.set("discord-prod", "top-secret-token")
    assert FileSecretStore(path).get("discord-prod") == "top-secret-token"
    assert path.stat().st_mode & 0o077 == 0
    assert "top-secret-token" in path.read_text()


def test_gateway_filtering_normalization_and_chunks(tmp_path: Path):
    from virtizai_core.db import Database
    db = Database(tmp_path / "state.db")
    db.open()
    db.execute("UPDATE discord_config SET enabled=1, allow_dms=0, allowed_servers_json=?, allowed_channels_json=?, allowed_users_json=? WHERE id='discord-default'", (json.dumps(["guild"]), json.dumps(["channel"]), json.dumps(["user"])))
    gateway = DiscordGateway(None, db, FileSecretStore(tmp_path / "secrets.json"))
    assert gateway.is_allowed(guild_id="guild", channel_id="channel", user_id="user")
    assert not gateway.is_allowed(guild_id="other", channel_id="channel", user_id="user")
    assert not gateway.is_allowed(guild_id=None, channel_id="channel", user_id="user")
    assert gateway.normalize_content("<@123> hello", "123", True) == "hello"
    assert gateway.normalize_content("hello", "123", True) is None
    assert gateway.response_chunks("x" * 4001) == ["x" * 2000, "x" * 2000, "x"]


@pytest.mark.asyncio
async def test_secret_api_redacts_value_and_discord_config_persists(tmp_path: Path):
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/v1/secrets/discord-dev", json={"value": "token-value"})
        assert response.status_code == 200
        assert "token-value" not in response.text
        assert (await client.get("/v1/secrets/discord-dev")).json() == {"reference": "discord-dev", "configured": True}
        saved = await client.put("/v1/discord/config", json={"enabled": True, "bot_secret_ref": "discord-dev", "allowed_servers": ["guild"], "allowed_channels": ["channel"], "require_mentions": False})
        assert saved.status_code == 200
        assert saved.json()["bot_secret_configured"] is True
        assert "token-value" not in saved.text
        assert saved.json()["allowed_servers"] == ["guild"]
        status = (await client.get("/v1/discord/status")).json()
        assert status["status"] in {"connecting", "error", "disabled"}


@pytest.mark.asyncio
async def test_disabled_gateway_never_connects(tmp_path: Path):
    app = create_app(AppConfig(tmp_path / "data", tmp_path / "workspace", tmp_path / "logs", tmp_path / "data" / "state.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = (await client.get("/v1/discord/status")).json()
        assert result["status"] == "disabled"


@pytest.mark.asyncio
async def test_gateway_uses_shared_adapter_session_and_sends_chunks(tmp_path: Path):
    from types import SimpleNamespace
    from virtizai_core.db import Database
    from virtizai_core.discord import DiscordReply
    db = Database(tmp_path / "state.db")
    db.open()
    db.execute("UPDATE discord_config SET enabled=1, allow_dms=0, require_mentions=0 WHERE id='discord-default'")
    calls = []
    class Adapter:
        async def handle_message(self, user_id, content, session_key=None, session_id=None, display_name="Discord user"):
            calls.append((user_id, content, session_key, display_name))
            return DiscordReply("reply", "session-1", {})
    class Channel:
        def __init__(self): self.sent=[]
        async def send(self, value): self.sent.append(value)
    channel = Channel()
    channel.id = 0
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=42, display_name="Owner"),
        guild=SimpleNamespace(id=7),
        channel=channel,
        content="hello",
    )
    gateway = DiscordGateway(Adapter(), db, FileSecretStore(tmp_path / "secrets.json"))
    assert await gateway.handle_message(message) is True
    assert calls == [("42", "hello", "guild:7:channel:0:user:42", "Owner")]
    assert message.channel.sent == ["reply"]


@pytest.mark.asyncio
async def test_gateway_ignores_explicit_dev_bot_mention(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    from virtizai_core.db import Database
    db = Database(tmp_path / "state.db")
    db.open()
    db.execute("UPDATE discord_config SET enabled=1, allow_dms=0, require_mentions=0 WHERE id='discord-default'")
    monkeypatch.setenv("VIRTIZAI_DISCORD_IGNORED_BOT_IDS", "1539768299389456465")
    class Adapter:
        async def handle_message(self, *args, **kwargs):
            raise AssertionError("ignored mention must not reach the adapter")
    message = SimpleNamespace(author=SimpleNamespace(bot=False, id=42), guild=SimpleNamespace(id=7), channel=SimpleNamespace(id=0), content="<@1539768299389456465> use dev")
    gateway = DiscordGateway(Adapter(), db, FileSecretStore(tmp_path / "secrets.json"))
    assert await gateway.handle_message(message) is False


@pytest.mark.asyncio
async def test_gateway_creates_persistent_thread_session(tmp_path):
    from types import SimpleNamespace
    from virtizai_core.db import Database
    from virtizai_core.discord import DiscordReply
    db = Database(tmp_path / "state.db")
    db.open()
    db.execute("UPDATE discord_config SET enabled=1, allow_dms=0, require_mentions=0 WHERE id='discord-default'")
    db.execute("INSERT INTO users(id, display_name) VALUES ('u1', 'Owner')")
    db.execute("INSERT INTO sessions(id, user_id, title) VALUES ('session-1', 'u1', 'Test')")
    db.execute("INSERT INTO interface_identities(id, user_id, interface_type, external_subject) VALUES ('i1', 'u1', 'discord', '42')")
    class Adapter:
        async def handle_message(self, *args, **kwargs):
            return DiscordReply("reply", "session-1", {"job_created": False})
    class Channel:
        id = 7
        parent_id = None
        def __init__(self): self.sent = []
        async def send(self, value): self.sent.append(value)
    class Thread(Channel):
        id = 8
        parent_id = 7
    class Message:
        id = 9
        content = "hello"
        guild = SimpleNamespace(id=7)
        author = SimpleNamespace(bot=False, id=42, display_name="Owner")
        channel = Channel()
        raw_mentions = []
        def __init__(self): self.reactions = []
        async def add_reaction(self, value): self.reactions.append(value)
        async def create_thread(self, name): return Thread()
    message = Message()
    gateway = DiscordGateway(Adapter(), db, FileSecretStore(tmp_path / "secrets.json"))
    assert await gateway.handle_message(message)
    assert message.reactions == ["👀"]
    mapping = db.fetch_one("SELECT thread_id, session_id FROM discord_thread_sessions")
    assert mapping["thread_id"] == "8"
    assert mapping["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_gateway_thread_reply_requires_mapping_and_reuses_session(tmp_path):
    from types import SimpleNamespace
    from virtizai_core.db import Database
    from virtizai_core.discord import DiscordReply
    import json
    db = Database(tmp_path / "state.db")
    db.open()
    db.execute("UPDATE discord_config SET enabled=1, allow_dms=0, require_mentions=0, allowed_channels_json=? WHERE id='discord-default'", (json.dumps(["7"]),))
    db.execute("INSERT INTO users(id, display_name) VALUES ('u1', 'Owner')")
    db.execute("INSERT INTO sessions(id, user_id, title) VALUES ('s1', 'u1', 'Thread')")
    db.execute("INSERT INTO interface_identities(id, user_id, interface_type, external_subject) VALUES ('i1', 'u1', 'discord', '42')")
    db.execute("INSERT INTO discord_thread_sessions(thread_id, session_id, guild_id, parent_channel_id, user_id) VALUES ('8', 's1', '7', '7', 'u1')")
    calls = []
    class Adapter:
        async def handle_message(self, user_id, content, session_key=None, session_id=None, display_name="Discord user"):
            calls.append((session_key, session_id))
            return DiscordReply("reply", "s1", {})
    class Channel:
        id = 8
        parent_id = 7
        async def send(self, value): pass
    message = SimpleNamespace(author=SimpleNamespace(bot=False, id=42, display_name="Owner"), guild=SimpleNamespace(id=7), channel=Channel(), content="follow up", raw_mentions=[])
    gateway = DiscordGateway(Adapter(), db, FileSecretStore(tmp_path / "secrets.json"))
    assert await gateway.handle_message(message)
    assert calls == [(None, "s1")]


@pytest.mark.asyncio
async def test_thread_cleanup_requires_scope_answers_and_explicit_confirmation(tmp_path):
    from types import SimpleNamespace
    from virtizai_core.db import Database

    db = Database(tmp_path / "state.db")
    db.open()
    db.execute("UPDATE discord_config SET enabled=1, allow_dms=0, require_mentions=0, allowed_servers_json=?, allowed_channels_json=? WHERE id='discord-default'", (json.dumps(["guild"]), json.dumps(["channel"])))

    class Thread:
        id = 99
        name = "test-thread"
        deleted = False

        async def delete(self, reason=None):
            self.deleted = True

    thread = Thread()

    class Guild:
        id = "guild"
        text_channels = []

        async def fetch_active_threads(self):
            return SimpleNamespace(threads=[thread])

    class Channel:
        id = "channel"
        parent_id = None

        def __init__(self):
            self.sent = []

        async def send(self, value):
            self.sent.append(value)

    class Adapter:
        async def handle_message(self, *args, **kwargs):
            raise AssertionError("cleanup flow must not invoke the model")

    channel = Channel()
    gateway = DiscordGateway(Adapter(), db, FileSecretStore(tmp_path / "secrets.json"))

    def message(content):
        return SimpleNamespace(
            author=SimpleNamespace(bot=False, id="user", display_name="Owner"),
            guild=Guild(), channel=channel, content=content, raw_mentions=[], id=1,
        )

    assert await gateway.handle_message(message("delete all threads in the server now"))
    assert "Before I delete anything" in channel.sent[-1]
    assert not thread.deleted
    assert await gateway.handle_message(message("one-time; include archived; preserve none"))
    assert "Proposed Discord cleanup scope" in channel.sent[-1]
    assert not thread.deleted
    assert await gateway.handle_message(message("CONFIRM DELETE"))
    assert thread.deleted
    assert "deleted 1" in channel.sent[-1]
