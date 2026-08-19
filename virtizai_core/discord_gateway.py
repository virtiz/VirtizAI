from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

try:
    import discord
except ImportError:  # pragma: no cover - dependency is declared for runtime installs
    discord = None

from .discord import DiscordAdapter

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayStatus:
    status: str
    last_error: str | None = None
    connected_at: str | None = None
    reconnects: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_error": self.last_error,
            "connected_at": self.connected_at,
            "reconnects": self.reconnects,
        }


class DiscordGateway:
    """Discord Gateway transport over the shared VirtizAI DiscordAdapter."""

    def __init__(self, adapter: DiscordAdapter, database, secret_store) -> None:
        self.adapter = adapter
        self.database = database
        self.secret_store = secret_store
        self.client = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._status = GatewayStatus("disabled")
        self._send_lock = asyncio.Lock()

    @staticmethod
    def _list(row, field: str) -> set[str]:
        try:
            return set(json.loads(row[field] or "[]"))
        except (KeyError, TypeError, json.JSONDecodeError):
            return set()

    def config(self):
        return self.database.fetch_one("SELECT * FROM discord_config WHERE id='discord-default'")

    def is_allowed(self, *, guild_id: str | None, channel_id: str, user_id: str) -> bool:
        row = self.config()
        if not row or not bool(row["enabled"]):
            return False
        if guild_id is None and not bool(row["allow_dms"]):
            return False
        servers = self._list(row, "allowed_servers_json")
        channels = self._list(row, "allowed_channels_json")
        users = self._list(row, "allowed_users_json")
        if servers and (guild_id is None or guild_id not in servers):
            return False
        if channels and channel_id not in channels:
            return False
        if users and user_id not in users:
            return False
        return True

    @staticmethod
    def normalize_content(content: str, bot_user_id: str | None = None, require_mentions: bool = False) -> str | None:
        text = content.strip()
        if bot_user_id:
            mention = f"<@{bot_user_id}>"
            nick_mention = f"<@!{bot_user_id}>"
            mentioned = mention in text or nick_mention in text
            if require_mentions and not mentioned:
                return None
            text = text.replace(mention, "").replace(nick_mention, "").strip()
        return text or None

    @staticmethod
    def response_chunks(content: str, limit: int = 2000) -> list[str]:
        if not content:
            return []
        return [content[i:i + limit] for i in range(0, len(content), limit)]

    def status(self) -> dict[str, Any]:
        return self._status.as_dict()

    async def _set_status(self, status: str, error: str | None = None) -> None:
        self._status = GatewayStatus(status, error, self._status.connected_at, self._status.reconnects)

    async def _run(self, token: str) -> None:
        if discord is None:
            await self._set_status("error", "discord.py is not installed")
            return
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        row = self.config()
        if row is not None and bool(row["allow_dms"]):
            intents.dm_messages = True
        self.client = discord.Client(intents=intents)

        @self.client.event
        async def on_ready():
            self._status = GatewayStatus("connected", None, datetime.now(timezone.utc).isoformat(), self._status.reconnects)

        @self.client.event
        async def on_disconnect():
            await self._set_status("disconnected")

        @self.client.event
        async def on_resumed():
            await self._set_status("connected")

        @self.client.event
        async def on_message(message):
            await self.handle_message(message)

        backoff = 1
        while not self._stop.is_set():
            try:
                await self._set_status("connecting")
                await self.client.start(token, reconnect=True)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # token is never included in the message
                self._status = GatewayStatus("error", type(exc).__name__, self._status.connected_at, self._status.reconnects + 1)
                await asyncio.wait_for(self._stop.wait(), timeout=min(backoff, 30))
                backoff = min(backoff * 2, 30)
            finally:
                if self.client and not self.client.is_closed():
                    await self.client.close()

    async def handle_message(self, message) -> bool:
        if getattr(message.author, "bot", False):
            return False
        guild = getattr(message, "guild", None)
        guild_id = str(guild.id) if guild else None
        channel_id = str(message.channel.id)
        user_id = str(message.author.id)
        if not self.is_allowed(guild_id=guild_id, channel_id=channel_id, user_id=user_id):
            return False
        row = self.config()
        text = self.normalize_content(
            getattr(message, "content", ""),
            str(getattr(self.client.user, "id", "")) if self.client and self.client.user else None,
            bool(row["require_mentions"]),
        )
        if text is None:
            return False
        key_prefix = f"guild:{guild_id}" if guild_id else "dm"
        session_key = f"{key_prefix}:channel:{channel_id}:user:{user_id}"
        display_name = getattr(message.author, "display_name", "Discord user")
        try:
            reply = await self.adapter.handle_message(user_id, text, session_key=session_key, display_name=display_name)
            async with self._send_lock:
                for chunk in self.response_chunks(reply.content):
                    await message.channel.send(chunk)
        except Exception as exc:
            await self._set_status("connected", type(exc).__name__)
            _LOG.warning("Discord message handling failed: %s", type(exc).__name__)
            return False
        return True

    async def start(self) -> None:
        await self.stop()
        row = self.config()
        if not row or not bool(row["enabled"]):
            await self._set_status("disabled")
            return
        reference = row["bot_secret_ref"]
        token = self.secret_store.get(reference) if reference else None
        if not token:
            await self._set_status("error", "bot token secret is not configured")
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(token), name="virtizai-discord-gateway")

    async def reload(self) -> None:
        await self.start()

    async def stop(self) -> None:
        self._stop.set()
        if self.client and not self.client.is_closed():
            await self.client.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, RuntimeError):
                pass
            self._task = None
        self.client = None
        if self._status.status not in {"error", "disabled"}:
            await self._set_status("disabled")
