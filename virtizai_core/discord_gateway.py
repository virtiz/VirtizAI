from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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

    def __init__(self, adapter: DiscordAdapter, database, secret_store, jobs=None) -> None:
        self.adapter = adapter
        self.database = database
        self.secret_store = secret_store
        self.jobs = jobs
        self.client = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._status = GatewayStatus("disabled")
        self._send_lock = asyncio.Lock()
        self.ignored_bot_ids = {item.strip() for item in os.environ.get("VIRTIZAI_DISCORD_IGNORED_BOT_IDS", "").split(",") if item.strip()}
        if self.jobs is not None:
            self.jobs.register_listener(self._on_job_complete)

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

    async def _on_job_complete(self, job: dict) -> None:
        payload = json.loads(job.get("payload_json") or "{}")
        notification = payload.get("notification") or {}
        if notification.get("interface") != "discord":
            return
        thread_id = notification.get("thread_id")
        session_id = job.get("session_id")
        result = json.loads(job.get("result_json") or "{}")
        status = job.get("status")
        summary = result.get("summary") or result.get("message") or result.get("reason") or status
        content = f"Codex worker {status}: {str(summary)[:1800]}"
        if session_id:
            self.adapter.interfaces.core.sessions.add_message(session_id, "assistant", content, {"route_id": "codex-worker"})
        if self.client and thread_id:
            channel = self.client.get_channel(int(thread_id))
            if channel:
                async with self._send_lock:
                    await channel.send(content)

    def _thread_mapping(self, thread_id: str):
        return self.database.fetch_one("SELECT * FROM discord_thread_sessions WHERE thread_id = ?", (thread_id,))

    async def _new_thread(self, message):
        try:
            await message.add_reaction("👀")
        except Exception:
            pass
        try:
            return await message.create_thread(name=f"VirtizAI • {str(message.content)[:60]}")
        except Exception:
            return None

    async def handle_message(self, message) -> bool:
        if getattr(message.author, "bot", False):
            return False
        raw_mentions = getattr(message, "raw_mentions", None)
        if raw_mentions is None:
            raw_mentions = [match.group(1) for match in re.finditer(r"<@!?(\d+)>", getattr(message, "content", ""))]
        if self.ignored_bot_ids.intersection(str(item) for item in raw_mentions):
            return False
        guild = getattr(message, "guild", None)
        guild_id = str(guild.id) if guild else None
        channel = message.channel
        channel_id = str(channel.id)
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
        mapping = self._thread_mapping(channel_id)
        is_thread = getattr(channel, "parent_id", None) is not None or mapping is not None
        if not is_thread:
            thread = await self._new_thread(message)
            if thread is not None:
                channel = thread
                channel_id = str(thread.id)
            session_key = (f"guild:{guild_id}:channel:{channel_id}:user:{user_id}" if thread is None else f"discord-thread:{channel_id}:user:{user_id}")
            session_id = None
        else:
            session_key = None
            session_id = mapping["session_id"] if mapping else None
        display_name = getattr(message.author, "display_name", "Discord user")
        try:
            reply = await self.adapter.handle_message(user_id, text, session_key=session_key, session_id=session_id, display_name=display_name)
            if reply.metadata.get("job_created"):
                job = self.database.fetch_one(
                    "SELECT id, payload_json FROM jobs WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                    (reply.session_id,),
                )
                if job:
                    payload = json.loads(job["payload_json"] or "{}")
                    payload.setdefault("notification", {}).update({"thread_id": channel_id, "interface": "discord"})
                    self.database.execute("UPDATE jobs SET payload_json = ? WHERE id = ?", (json.dumps(payload), job["id"]))
            if not mapping:
                mapped_user = self.database.fetch_one("SELECT user_id FROM interface_identities WHERE interface_type='discord' AND external_subject=?", (user_id,))
                if mapped_user:
                    self.database.execute(
                        """INSERT OR REPLACE INTO discord_thread_sessions
                           (thread_id, session_id, guild_id, parent_channel_id, starter_message_id, user_id)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (channel_id, reply.session_id, guild_id, str(message.channel.id), str(message.id), mapped_user["user_id"]),
                    )
            async with self._send_lock:
                for chunk in self.response_chunks(reply.content):
                    await channel.send(chunk)
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
