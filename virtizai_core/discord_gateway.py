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

    def __init__(self, adapter: DiscordAdapter, database, secret_store, jobs=None, events=None) -> None:
        self.adapter = adapter
        self.database = database
        self.secret_store = secret_store
        self.jobs = jobs
        self.events = events
        if self.events is not None:
            self.events.subscribe(self._on_operational_event)
        self.client = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._status = GatewayStatus("disabled")
        self._intentional_shutdown = False
        self._send_lock = asyncio.Lock()
        # Destructive Discord actions are deliberately confirmation-gated.  A
        # pending scope is bound to one guild/user and is never inferred from
        # model output or shared across users.
        self._thread_cleanup_pending: dict[tuple[str, str], dict[str, Any]] = {}
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

    @staticmethod
    def _thread_cleanup_requested(text: str) -> bool:
        tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
        return bool(tokens & {"delete", "remove", "prune", "clean"}) and bool(tokens & {"thread", "threads"}) and bool(tokens & {"server", "guild", "discord"})

    @staticmethod
    def _thread_action_candidate(text: str) -> bool:
        tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
        return bool(tokens & {"delete", "remove", "prune", "clean", "cleanup"}) and bool(tokens & {"thread", "threads", "server", "guild", "discord"})

    @staticmethod
    def _cleanup_answers(text: str) -> dict[str, str]:
        lowered = text.lower()
        compact = lowered.strip()
        answers: dict[str, str] = {}
        if compact in {"1", "one", "one-time", "one time"} or any(term in lowered for term in ("one-time", "one time", "once", "now only")):
            answers["mode"] = "one-time cleanup"
        elif compact in {"2", "reusable"} or any(term in lowered for term in ("reusable", "command", "future", "keep enabled")):
            answers["mode"] = "reusable command"
        if compact in {"yes", "y"} or any(term in lowered for term in ("archived", "all threads", "every thread", "include all")):
            answers["archived"] = "including archived threads"
        elif compact in {"no", "n", "active", "active only"} or any(term in lowered for term in ("active only", "not archived", "exclude archived")):
            answers["archived"] = "active threads only"
        if any(term in lowered for term in ("none", "no exclusions", "nothing to preserve", "preserve nothing")):
            answers["preserve"] = "none"
        elif "preserve" in lowered:
            value = lowered.split("preserve", 1)[1].strip(" :,-")
            if value:
                answers["preserve"] = value
        return answers

    async def _send_direct(self, channel, content: str) -> None:
        async with self._send_lock:
            for chunk in self.response_chunks(content):
                await channel.send(chunk)

    @staticmethod
    def _inspection_kind(text: str) -> str | None:
        tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
        if {"how", "many"} <= tokens and tokens & {"thread", "threads"} and tokens & {"server", "guild", "discord"}:
            return "actual_thread_count"
        if (tokens & {"list", "show", "which"}) and tokens & {"thread", "threads"}:
            return "thread_list"
        if tokens & {"mapped", "mapping"} and tokens & {"thread", "threads", "sessions"}:
            return "mapped_thread_count"
        if tokens & {"gateway", "connection", "connected"} and tokens & {"status", "healthy", "online", "state"}:
            return "gateway_status"
        if tokens & {"channel", "channels"} and tokens & {"monitoring", "configured", "watching"}:
            return "scope"
        return None

    async def _discord_threads(self, guild) -> tuple[list[Any], str | None]:
        try:
            fetcher = getattr(guild, "fetch_active_threads", None)
            if fetcher is not None:
                result = await fetcher()
                if hasattr(result, "threads"):
                    return list(result.threads or []), None
                if isinstance(result, tuple):
                    return list(result[0] or []), None
                return list(result or []), None
            # discord.py 2.x exposes the gateway-synchronized cache as
            # Guild.active_threads; Guild.fetch_active_threads is not a method.
            return list(getattr(guild, "active_threads", []) or []), None
        except Exception as exc:
            _LOG.warning("Discord active-thread inspection failed: %s", type(exc).__name__, exc_info=True)
            return [], f"{type(exc).__name__}: {str(exc)[:180]}"

    async def _inspection_response(self, kind: str, guild, guild_id: str | None) -> str:
        if kind == "gateway_status":
            return f"Discord gateway status: {self._status.status}."
        row = self.config()
        if kind == "scope":
            servers = sorted(self._list(row, "allowed_servers_json")) if row else []
            channels = sorted(self._list(row, "allowed_channels_json")) if row else []
            return f"Configured Discord scope: guilds={len(servers) or 'all allowed'}; channels={len(channels) or 'all allowed'}."
        mapped = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM discord_thread_sessions WHERE guild_id=?",
            (guild_id,),
        ) if guild_id else {"count": 0}
        if kind == "mapped_thread_count":
            return f"VirtizAI-mapped Discord threads in this guild: {int(mapped['count'])}."
        threads, error = await self._discord_threads(guild)
        if error:
            return f"Actual Discord thread inventory is unavailable: {error}. VirtizAI-mapped threads: {int(mapped['count'])}."
        if kind == "thread_list":
            names = [f"{getattr(item, 'name', 'unnamed')} ({getattr(item, 'id', 'unknown')})" for item in threads[:25]]
            return "Actual active Discord threads (authoritative gateway data): " + (", ".join(names) if names else "none") + f". VirtizAI-mapped threads: {int(mapped['count'])}."
        return f"Actual active Discord threads: {len(threads)} (authoritative gateway data). VirtizAI-mapped threads: {int(mapped['count'])}. Archived-thread inventory is not included unless Discord exposes it to this bot."

    async def _thread_cleanup_flow(self, message, guild_id: str, user_id: str, text: str, recognized: bool = False) -> bool:
        key = (guild_id, user_id)
        pending = self._thread_cleanup_pending.get(key)
        if pending is None and not recognized and not self._thread_cleanup_requested(text):
            return False
        if pending is None:
            self._thread_cleanup_pending[key] = {"stage": "questions", "channel_id": str(message.channel.id)}
            await self._send_direct(message.channel, "Before I delete anything, please clarify:\n1. Is this a one-time cleanup now, or should this become a reusable command?\n2. Should I include all threads, including archived threads?\n3. Are there any threads I should preserve?\n\nI will show the exact scope and ask for explicit confirmation before deleting anything.")
            return True
        answers = dict(pending.get("answers") or {})
        answers.update(self._cleanup_answers(text))
        pending["answers"] = answers
        if pending["stage"] == "questions":
            missing = [(label, field, question) for label, field, question in (
                ("one-time or reusable", "mode", "Is this a one-time cleanup (`1`) or reusable command (`2`)?"),
                ("include archived or active only", "archived", "Should I include archived threads, or delete active threads only?"),
                ("threads to preserve (or none)", "preserve", "Are there any threads to preserve? Reply with their IDs/names, or `none`."),
            ) if field not in answers]
            if missing:
                await self._send_direct(message.channel, missing[0][2])
                return True
            pending["stage"] = "confirmation"
            await self._send_direct(message.channel, "Proposed Discord cleanup scope:\n- Guild: this configured server\n- Threads: " + answers["archived"] + "\n- Preserve: " + answers["preserve"] + "\n- Mode: " + answers["mode"] + "\n\nReply `CONFIRM DELETE` to proceed, or `CANCEL` to abandon. No threads have been deleted.")
            return True
        if pending["stage"] == "confirmation":
            if re.search(r"\bcancel\b", text, re.IGNORECASE):
                self._thread_cleanup_pending.pop(key, None)
                await self._send_direct(message.channel, "Cancelled. No Discord threads were deleted.")
                return True
            if not re.search(r"\bconfirm\b.*\bdelete\b|\bdelete\b.*\bconfirm\b", text, re.IGNORECASE):
                await self._send_direct(message.channel, "No deletion occurred. Reply `CONFIRM DELETE` to proceed, or `CANCEL`.")
                return True
            self._thread_cleanup_pending.pop(key, None)
            guild = getattr(message, "guild", None)
            threads = []
            try:
                fetcher = getattr(guild, "fetch_active_threads", None)
                if fetcher is not None:
                    active = await fetcher()
                    threads.extend(list(getattr(active, "threads", active) or []))
                else:
                    threads.extend(list(getattr(guild, "active_threads", []) or []))
            except Exception as exc:
                _LOG.warning("Discord cleanup thread enumeration failed: %s", type(exc).__name__, exc_info=True)
                await self._send_direct(message.channel, f"I could not enumerate active Discord threads ({type(exc).__name__}); nothing was deleted.")
                return True
            if answers["archived"] == "including archived threads":
                for channel in getattr(guild, "text_channels", []) or []:
                    try:
                        async for thread in channel.archived_threads(limit=None):
                            threads.append(thread)
                    except Exception:
                        continue
            preserve = {item.strip() for item in answers["preserve"].split(",") if item.strip() and item.strip() != "none"}
            deleted = 0
            failed = 0
            for thread in {str(getattr(item, "id", "")): item for item in threads}.values():
                if not getattr(thread, "id", None) or str(thread.id) in preserve or str(getattr(thread, "name", "")) in preserve:
                    continue
                try:
                    await thread.delete(reason="VirtizAI confirmed Discord thread cleanup")
                    deleted += 1
                except Exception:
                    failed += 1
            await self._send_direct(message.channel, f"Discord thread cleanup completed: deleted {deleted}, failed {failed}, preserved {len(preserve)}. This is the complete result; no deletion is claimed for failures.")
            return True
        return True

    def status(self) -> dict[str, Any]:
        return self._status.as_dict()

    async def _set_status(self, status: str, error: str | None = None) -> None:
        previous = self._status.status
        self._status = GatewayStatus(status, error, self._status.connected_at, self._status.reconnects)
        if self.events is not None and status != previous and not (
            self._intentional_shutdown and status in {"disconnected", "disabled"}
        ):
            await self.events.transition("gateway", "discord", "Discord gateway", status, error, "error" if status == "error" else "info", initial=previous == "disabled")

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
            await self._set_status("connected")
            self._status = GatewayStatus("connected", None, datetime.now(timezone.utc).isoformat(), self._status.reconnects)
            await self._retry_pending_alerts()

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

    async def _retry_pending_alerts(self) -> None:
        row = self.config()
        channel_id = row["alert_channel_id"] if row and "alert_channel_id" in row.keys() else None
        if not channel_id or not self.client:
            return
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            return
        pending = self.database.fetch_all(
            "SELECT * FROM operational_events WHERE notification_status IN ('pending','failed') ORDER BY created_at LIMIT 20"
        )
        for event in pending:
            try:
                await channel.send(self._alert_text(dict(event)))
                self.database.execute("UPDATE operational_events SET notification_status='delivered' WHERE id=?", (event["id"],))
            except Exception:
                break

    @staticmethod
    def _alert_text(event: dict) -> str:
        icon = "🟢" if event["new_state"] in {"healthy", "available", "connected", "succeeded"} else ("🟡" if event["new_state"] in {"degraded", "connecting"} else "🔴")
        title = event["new_state"].replace("_", " ").title()
        text = f"{icon} {event['component_type'].title()} {title}\n{event['component_name']}"
        if event.get("reason"):
            text += f"\nReason: {str(event['reason'])[:500]}"
        return text

    async def _on_operational_event(self, event: dict) -> None:
        row = self.config()
        channel_id = row["alert_channel_id"] if row and "alert_channel_id" in row.keys() else None
        if not channel_id or not self.client:
            return
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(self._alert_text(event))
            self.database.execute("UPDATE operational_events SET notification_status='delivered' WHERE id=?", (event["id"],))
        except Exception:
            self.database.execute("UPDATE operational_events SET notification_status='failed' WHERE id=?", (event["id"],))

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
            self.adapter.interfaces.core.sessions.add_message(session_id, "assistant", content, {"execution_type": "worker", "role": "codex_worker", "worker": "Codex CLI worker", "job_id": job.get("id"), "route_id": "codex-worker", "task_class": "hard"})
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
        mapping = self._thread_mapping(channel_id)
        # Threads are authorized through their persisted parent mapping, never by
        # globally allowing arbitrary thread IDs.
        if mapping:
            identity = self.database.fetch_one(
                "SELECT user_id FROM interface_identities WHERE interface_type='discord' AND external_subject=?",
                (user_id,),
            )
            if (mapping["guild_id"] and mapping["guild_id"] != guild_id) or not identity or identity["user_id"] != mapping["user_id"]:
                return False
            auth_channel_id = mapping["parent_channel_id"]
        else:
            auth_channel_id = channel_id
        if not self.is_allowed(guild_id=guild_id, channel_id=auth_channel_id, user_id=user_id):
            return False
        row = self.config()
        text = self.normalize_content(
            getattr(message, "content", ""),
            str(getattr(self.client.user, "id", "")) if self.client and self.client.user else None,
            bool(row["require_mentions"]),
        )
        if text is None:
            return False
        inspection = self._inspection_kind(text)
        if inspection and guild is not None:
            await self._send_direct(channel, await self._inspection_response(inspection, guild, guild_id))
            return True
        if guild_id is not None and ((guild_id, user_id) in self._thread_cleanup_pending or self._thread_action_candidate(text)):
            if (guild_id, user_id) in self._thread_cleanup_pending:
                await self._thread_cleanup_flow(message, guild_id, user_id, text)
                return True
            # Let the configured Secretary interpret natural language first;
            # the result is only a typed proposal.  The cleanup flow still
            # performs scope checks and explicit confirmation before deletion.
            planner = getattr(getattr(self.adapter, "interfaces", None), "core", None)
            planned = None
            if planner is not None and hasattr(planner, "plan_operational_action"):
                try:
                    planned = await planner.plan_operational_action(text)
                except Exception:
                    planned = None
            if planned and planned.get("action") == "discord_thread_cleanup":
                await self._thread_cleanup_flow(message, guild_id, user_id, text, recognized=True)
                return True
            if await self._thread_cleanup_flow(message, guild_id, user_id, text):
                return True
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
        self._intentional_shutdown = True
        await self.stop()
        self._intentional_shutdown = False
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
        self._intentional_shutdown = True
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
