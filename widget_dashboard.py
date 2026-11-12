"""Private operator dashboard using the public Bot API 10.4 widget methods.

The SDK may not know widget types yet; do_api_request and Update.api_kwargs
are its public compatibility interfaces. The widget never receives credentials.
"""

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import secrets
import time
import zlib

from telegram.error import BadRequest, TelegramError


class WidgetDashboard:
    PAGE_SIZE = 2
    DATA_LIMIT = 1024
    ACTIVE_SECONDS = 300
    REFRESH_SECONDS = 30
    CONFIRM_SECONDS = 60
    QUERY_HISTORY_SECONDS = 300
    QUERY_HISTORY_LIMIT = 512

    def __init__(self, service):
        self.service = service
        self._active = {}
        self._locks = {}
        self._confirmations = {}
        self._transition = None
        self._notice = ""
        self._queries = OrderedDict()
        self._transition_tasks = set()

    @property
    def bot(self):
        return self.service.telegram_bot.bot

    @staticmethod
    def _key(user_id):
        return f"widget_dashboard:{user_id}"

    @staticmethod
    def _html():
        source = (Path(__file__).resolve().parent / "assets" / "dashboard.html").read_text(encoding="utf-8")
        raw = source.encode("utf-8")
        if not raw or len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
            raise ValueError("Widget exceeds Telegram HTML limits")
        return source, hashlib.sha256(raw).hexdigest()[:16]

    def _saved(self, user_id):
        try:
            record = json.loads(self.service.database.get_setting(self._key(user_id)) or "null")
            if (not isinstance(record, dict) or type(record.get("message_id")) is not int
                    or record["message_id"] <= 0 or not isinstance(record.get("layout"), str)):
                return None
            return record
        except (ValueError, TypeError):
            return None

    def _save(self, user_id, record):
        self.service.database.set_setting(self._key(user_id), json.dumps(record, separators=(",", ":")))

    def _allowed(self, user_id, chat_id, chat_type):
        return (type(user_id) is int and user_id > 0
                and user_id in self.service.config.telegram_allowed_user_ids
                and type(chat_id) is int and chat_id == user_id and chat_type == "private")

    def group_link(self):
        group = str(self.service.config.telegram_group_id)
        return f"https://t.me/c/{group[4:]}/1" if re.fullmatch(r"-100[0-9]+", group) else ""

    def _topics(self):
        topics = self.service.topic_manager.get_all_topics().values()
        return sorted((t for t in topics if type(t.get("topic_id")) is int and t["topic_id"] > 0
                       and type(t.get("invoice_id")) is int and t["invoice_id"] > 0),
                      key=lambda t: str(t.get("created_at", "")), reverse=True)

    @staticmethod
    def _clock(value):
        if not isinstance(value, datetime):
            return "—"
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%H:%M UTC")

    def render_state(self, view="home", page=0, *, user_id=None):
        if view not in ("home", "chats", "settings", "status"):
            view = "home"
        topics = self._topics()
        pages = max(1, math.ceil(len(topics) / self.PAGE_SIZE))
        page = max(0, min(page if type(page) is int else 0, pages - 1))
        checks = [getattr(self.service, name, None)
                  for name in ("last_sales_check", "last_chat_check", "last_message_check")]
        recent = [v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
                  for v in checks if isinstance(v, datetime)]
        healthy = bool(recent and (datetime.now(timezone.utc) - max(recent)).total_seconds() < 180)
        panel = self.service.operator_panel
        state = {
            "v": 1, "view": view, "sync": bool(self.service.sync_enabled),
            "notify": panel.notifications_enabled(), "chats": len(topics),
            "commands": len(self.service.autoresponder.get_triggers()),
            "plugins": sum(bool(p.get("enabled")) and not p.get("error") for p in panel.plugins.list_plugins()),
            "queue": len(getattr(self.service, "pending_messages", [])),
            "group": self.group_link(), "updated": datetime.now(timezone.utc).strftime("%H:%M UTC"),
            "health": "ok" if healthy else "waiting", "page": page, "pages": pages,
            "items": [], "checks": [self._clock(v) for v in checks],
            "confirm": "", "busy": self._transition is not None and not self._transition.done(),
        }
        if self._notice:
            state["notice"] = self._notice
        confirmation = self._confirmations.get(user_id)
        if confirmation and confirmation["expires"] > time.monotonic():
            state["confirm"] = confirmation["nonce"]
        if view == "chats" and state["group"]:
            base = state["group"].rsplit("/", 1)[0]
            state["items"] = [
                {"id": t["invoice_id"], "name": f"Диалог #{t['invoice_id']}",
                 "url": f"{base}/{t['topic_id']}"}
                for t in topics[page * self.PAGE_SIZE:(page + 1) * self.PAGE_SIZE]
            ]
        return state

    @classmethod
    def encode_state(cls, state):
        data = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        if len(data.encode("utf-8")) > cls.DATA_LIMIT:
            # Never silently truncate JSON or misreport page lengths.
            raise ValueError("Widget state exceeds Telegram limit")
        return data

    async def _request(self, method, data):
        return await asyncio.wait_for(self.bot.do_api_request(
            method, api_kwargs=data, read_timeout=6, connect_timeout=3, write_timeout=3,
        ), timeout=8)

    async def _publish(self, user_id, record):
        payload = self.encode_state(self.render_state(record.get("view", "home"), record.get("page", 0), user_id=user_id))
        await self._request("editMessageWidgetData", {"chat_id": user_id,
                            "message_id": record["message_id"], "chat_data": payload})
        return payload

    def _keyboard(self):
        link = self.group_link()
        return {"inline_keyboard": [[{"text": "Открыть группу магазина", "url": link}]]} if link else None

    async def open(self, update):
        chat, user = getattr(update, "effective_chat", None), getattr(update, "effective_user", None)
        if not chat or getattr(chat, "type", None) != "private":
            return False
        if not user or not self._allowed(user.id, chat.id, chat.type):
            await update.effective_message.reply_text("Панель доступна только операторам магазина.")
            return True
        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            source, layout = self._html()
            record = self._saved(user.id)
            self._confirmations.pop(user.id, None)
            if record and record["layout"] == layout:
                record.update(view="home", page=0)
                try:
                    await self._publish(user.id, record)
                except BadRequest:
                    record = None
                else:
                    self._save(user.id, record)
                    self._active[user.id] = time.monotonic()
                    await update.effective_message.reply_text(
                        "Панель обновлена. Нажмите на сообщение выше, чтобы вернуться к ней.",
                        reply_to_message_id=record["message_id"], allow_sending_without_reply=True)
                    return True
            caption = "GGSEL SELLER · управление магазином. Если виджет не отображается, обновите Telegram. Обычное меню доступно по /menu в группе."
            request = {"chat_id": user.id, "html": source, "allow_queries": True,
                       "chat_data": self.encode_state(self.render_state()), "caption": caption}
            if self._keyboard():
                request["reply_markup"] = self._keyboard()
            try:
                result = await self._request("sendWidget", request)
            except BadRequest:
                logging.warning("Telegram widget unavailable; operator receives standard navigation")
                await self.bot.send_message(chat_id=user.id,
                    text="HTML-панель сейчас недоступна. Откройте группу магазина и отправьте /menu.",
                    reply_markup=self._keyboard())
                return True
            if (not isinstance(result, dict) or type(result.get("message_id")) is not int
                    or result["message_id"] <= 0):
                raise RuntimeError("Telegram did not return a widget message identifier")
            record = {"message_id": result["message_id"], "layout": layout, "view": "home", "page": 0}
            self._save(user.id, record)
            self._active[user.id] = time.monotonic()
            return True

    @staticmethod
    def _query(update):
        direct = getattr(update, "widget_query", None)
        if direct is not None:
            return direct if isinstance(direct, dict) else direct.to_dict()
        return getattr(update, "api_kwargs", {}).get("widget_query")

    async def _answer(self, query_id, payload):
        await self._request("answerWidgetQuery", {"widget_query_id": query_id, "data": payload})

    def _remember_query(self, query_id):
        """Reject redelivery before applying settings; retain no query contents."""
        now = time.monotonic()
        while self._queries:
            first, seen = next(iter(self._queries.items()))
            if now - seen < self.QUERY_HISTORY_SECONDS:
                break
            self._queries.pop(first)
        if query_id in self._queries:
            return False
        self._queries[query_id] = now
        while len(self._queries) > self.QUERY_HISTORY_LIMIT:
            self._queries.popitem(last=False)
        return True

    async def _finish_transition(self, user_id, record, pause, ready):
        cancelled = False
        try:
            await ready.wait()
            await (self.service.pause_sync() if pause else self.service.start_sync())
            self._notice = "Синхронизация приостановлена" if pause else "Синхронизация включена"
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            self._notice = "Не удалось изменить состояние. Проверьте меню группы."
            logging.exception("Widget sync transition failed")
        finally:
            self._transition = None
            if not cancelled:
                lock = self._locks.setdefault(user_id, asyncio.Lock())
                async with lock:
                    current = self._saved(user_id)
                    if (current and current["message_id"] == record["message_id"]
                            and current["layout"] == record["layout"]
                            and user_id in self.service.config.telegram_allowed_user_ids):
                        try:
                            # Navigation may have changed while GGsel was busy.
                            # Refresh the current view under the same write lock.
                            await self._publish(user_id, current)
                        except (TelegramError, TimeoutError):
                            logging.warning("Could not refresh widget after sync transition")

    async def close(self):
        """Cancel widget work before shutting down the Telegram transport."""
        tasks = list(self._transition_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._transition = None
        self._active.clear()
        self._confirmations.clear()

    async def handle_update(self, update, context=None):
        query = self._query(update)
        if query is None:
            return False
        if not isinstance(query, dict) or not isinstance(query.get("id"), str) or not query["id"]:
            return True
        user, chat = query.get("from") or {}, query.get("chat") or {}
        user_id = user.get("id") if isinstance(user, dict) else None
        allowed = (isinstance(chat, dict) and self._allowed(user_id, chat.get("id"), chat.get("type")))
        record = self._saved(user_id) if allowed else None
        kind, action = query.get("type"), query.get("data")
        if (not record or type(query.get("message_id")) is not int or query["message_id"] != record["message_id"] or query.get("widget_id")
                or not isinstance(kind, dict) or kind.get("type") != "data"
                or not isinstance(action, str) or not 1 <= len(action.encode("utf-8")) <= 64):
            await self._answer(query["id"], '{"error":"Панель недоступна. Откройте /menu в личном чате бота."}')
            return True
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await self._answer(query["id"], '{"error":"Предыдущее действие ещё выполняется"}')
            return True
        async with lock:
            if not self._remember_query(query["id"]):
                await self._answer(query["id"], '{"error":"Действие уже обработано. Обновите панель."}')
                return True
            self._active[user_id] = time.monotonic()
            transition = None
            try:
                if action in ("view:home", "view:settings", "view:status"):
                    record.update(view=action[5:], page=0)
                    self._confirmations.pop(user_id, None)
                elif re.fullmatch(r"view:chats:[0-9]{1,6}", action):
                    record.update(view="chats", page=int(action.rsplit(":", 1)[1]))
                    self._confirmations.pop(user_id, None)
                elif action in ("notify:on", "notify:off"):
                    self.service.database.set_setting("telegram_notifications_enabled", "true" if action == "notify:on" else "false")
                elif action == "sync:pause":
                    if self._transition is None:
                        self._confirmations[user_id] = {"message_id": record["message_id"],
                            "nonce": secrets.token_hex(8), "expires": time.monotonic() + self.CONFIRM_SECONDS}
                elif action == "sync:resume":
                    transition = False
                elif action.startswith("confirm:"):
                    confirmation = self._confirmations.pop(user_id, None)
                    if (not confirmation or confirmation["expires"] <= time.monotonic()
                            or confirmation["message_id"] != record["message_id"]
                            or not secrets.compare_digest(confirmation["nonce"], action[8:])):
                        raise ValueError("Подтверждение устарело. Повторите действие.")
                    transition = True
                elif action != "refresh":
                    raise ValueError("Неизвестное действие")
                self._save(user_id, record)
                state = self.render_state(record.get("view", "home"), record.get("page", 0), user_id=user_id)
                if transition is not None and self._transition is None:
                    state.update(busy=True, notice="Изменяем состояние синхронизации…")
                payload = self.encode_state(state)
            except ValueError as exc:
                await self._answer(query["id"], json.dumps({"error": str(exc)}, ensure_ascii=False))
                return True
            # Answer first: a pause can wait for in-flight GGsel requests longer
            # than Telegram's 15 second deadline. A separate task finishes it.
            await self._answer(query["id"], payload)
            ready = None
            if transition is not None and self._transition is None:
                ready = asyncio.Event()
                self._transition = asyncio.create_task(self._finish_transition(user_id, record.copy(), transition, ready))
                self._transition_tasks.add(self._transition)
                self._transition.add_done_callback(self._transition_tasks.discard)
            try:
                await self._request("editMessageWidgetData", {"chat_id": user_id,
                                    "message_id": record["message_id"], "chat_data": payload})
            except (TelegramError, TimeoutError):
                logging.warning("Widget answer delivered but persisted state refresh failed")
            finally:
                if ready is not None:
                    ready.set()
            return True

    async def refresh_loop(self):
        """Refresh only dashboards used recently; no messages are created here."""
        while self.service.running:
            await asyncio.sleep(self.REFRESH_SECONDS)
            if not self.service.running:
                return
            for user_id, seen in list(self._active.items()):
                if user_id not in self.service.config.telegram_allowed_user_ids or time.monotonic() - seen > self.ACTIVE_SECONDS:
                    self._active.pop(user_id, None)
                    continue
                lock = self._locks.setdefault(user_id, asyncio.Lock())
                if lock.locked():
                    continue
                async with lock:
                    record = self._saved(user_id)
                    if not record:
                        continue
                    try:
                        await self._publish(user_id, record)
                    except (TelegramError, TimeoutError):
                        self._active.pop(user_id, None)
                        logging.warning("Widget background refresh stopped after delivery failure")
