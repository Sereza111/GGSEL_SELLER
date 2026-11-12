"""Telegram operator controls; no credentials or customer writes in this module."""

import copy
import io
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from autoresponder import AutoResponder
from plugin_manager import PluginManager


class OperatorPanel:
    PAGE_SIZE = 8
    MAX_CONFIG_BYTES = 262144
    INPUT_TTL = 600
    EXPORT_FORMAT = "ggsel-helper-settings"

    def __init__(self, service):
        self.service = service
        self.plugins = PluginManager(service.database)
        self._imports = {}

    @staticmethod
    def button(text, action, style=None):
        if style is None:
            style = {"panel:chats:0": "primary", "panel:pause": "danger",
                     "panel:resume": "success"}.get(action)
            if action.startswith("panel:config_apply:"):
                style = "primary"
        if style not in (None, "primary", "success", "danger"):
            raise ValueError("Недопустимый стиль кнопки")
        # PTB versions before native style support forward this Bot API field.
        # Older Telegram clients keep the same text and callback behavior.
        return InlineKeyboardButton(text, callback_data=action,
                                    api_kwargs={"style": style} if style else None)

    def _dashboard_link(self):
        username = getattr(self.service.telegram_bot, "public_username", None)
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
            return None
        return f"https://t.me/{username}?start=dashboard"

    def notifications_enabled(self):
        return self.service.database.get_setting("telegram_notifications_enabled") != "false"

    async def plugin_response(self, content):
        return await self.plugins.find_response(content)

    def main_menu(self):
        active = bool(self.service.sync_enabled)
        notifications = self.notifications_enabled()
        text = (
            "GGsel · Рабочее место\n\n"
            f"{'🟢' if active else '⏸'} Синхронизация {'включена' if active else 'остановлена'}\n"
            f"{'🔔' if notifications else '🔕'} Уведомления о сообщениях "
            f"{'включены' if notifications else 'выключены'}\n"
            f"💬 Чатов: {len(self._topics())} · Команд: {len(self.service.autoresponder.get_triggers())}\n"
            f"⚙️ Автоответы {'включены' if self.service.autoresponder.is_enabled() else 'выключены'}\n\n"
            "Чтобы ответить покупателю, откройте его тему через «Чаты и ответы» и напишите текст."
        )
        b = self.button
        keyboard = []
        dashboard_link = self._dashboard_link()
        if dashboard_link:
            keyboard.append([InlineKeyboardButton("✦ Открыть панель", url=dashboard_link,
                                                  api_kwargs={"style": "primary"})])
            text += "\nПанель откроется в личном чате."
        keyboard += [
            [b("💬 Чаты и ответы", "panel:chats:0"), b("🩺 Состояние", "panel:status")],
            [b("⌨️ Команды", "auto_triggers"), b("⚙️ Автоответы", "auto_menu")],
            [b("🔔 Уведомления", "panel:notifications"), b("🧩 Плагины", "panel:plugins:0")],
            [b("🗂 Конфигурация", "panel:config")],
            [b("💰 Кошелёк", "check_balance"), b("📊 Статистика", "stats")],
            [b("❓ Помощь", "panel:help"), b("Закрыть", "close")],
        ]
        return text, keyboard

    def _authorized(self, update):
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        return bool(chat and user and chat.id == self.service.config.telegram_group_id
                    and user.id in self.service.config.telegram_allowed_user_ids)

    @staticmethod
    def _identity(update):
        message = getattr(update, "effective_message", None)
        return (update.effective_chat.id, update.effective_user.id,
                getattr(message, "message_thread_id", None))

    def cancel_input(self, update):
        if not self._authorized(update):
            return
        identity = self._identity(update)
        for key in list(self._imports):
            if key[:2] == identity[:2]:
                self._imports.pop(key, None)

    def has_pending_input(self, update):
        if not self._authorized(update):
            return False
        state = self._imports.get(self._identity(update))
        return bool(state and state["expires"] > time.monotonic())

    async def _render(self, update, text, keyboard=None):
        if keyboard is None:
            keyboard = [[self.button("◀️ В меню", "panel:main")]]
        query = update.callback_query
        await self.service.telegram_bot.edit_message(
            query.message.message_id, query.message.chat.id, text[:4000], keyboard)

    def _topics(self):
        topics = self.service.topic_manager.get_all_topics()
        return sorted(
            [t for t in topics.values() if t.get("topic_id") and t.get("invoice_id")],
            key=lambda t: str(t.get("created_at", "")), reverse=True)

    def _topic_link(self, topic_id):
        group = str(self.service.config.telegram_group_id)
        if not group.startswith("-100"):
            return None
        try:
            topic_id = int(topic_id)
        except (ValueError, TypeError):
            return None
        return f"https://t.me/c/{group[4:]}/{topic_id}" if topic_id > 0 else None

    async def _chats(self, update, page):
        topics = self._topics()
        page = max(0, min(page, max(0, (len(topics) - 1) // self.PAGE_SIZE)))
        text = (
            f"💬 Чаты покупателей: {len(topics)}\n\n"
            "Нажмите на покупателя. Обычный текст в его теме отправляется в GGsel. "
            "Реакция 🔥 означает, что API принял ответ.\n"
            "История: /history в теме. Команды управления покупателю не отправляются."
        )
        if not topics:
            text += ("\n\nПока нет связанных переписок. Они появляются после заказа или "
                     "обнаружения переписки в API GGsel. Пустой список сам по себе не означает сбой.")
        keyboard = []
        for topic in topics[page * self.PAGE_SIZE:(page + 1) * self.PAGE_SIZE]:
            label = topic.get("topic_name") or topic.get("email") or f"Заказ {topic['invoice_id']}"
            link = self._topic_link(topic["topic_id"])
            if link:
                keyboard.append([InlineKeyboardButton(f"↗️ {str(label)[:52]}", url=link)])
        nav = []
        if page:
            nav.append(self.button("◀️", f"panel:chats:{page - 1}"))
        if (page + 1) * self.PAGE_SIZE < len(topics):
            nav.append(self.button("▶️", f"panel:chats:{page + 1}"))
        if nav:
            keyboard.append(nav)
        keyboard += [[self.button("🔄 Обновить список", f"panel:chats:{page}")],
                     [self.button("◀️ В меню", "panel:main")]]
        await self._render(update, text, keyboard)

    async def _status(self, update):
        service = self.service
        auth = getattr(service, "last_auth_time", None)
        auth_text = auth.strftime("%d.%m %H:%M:%S") if auth else "ещё не подтверждена"
        def checked(name):
            value = getattr(service, name, None)
            return value.strftime("%d.%m %H:%M:%S") if value else "ещё не завершалась"
        text = (
            "🩺 Состояние бота\n\n"
            f"Синхронизация GGsel: {'включена' if service.sync_enabled else 'остановлена'}\n"
            f"Последняя успешная авторизация GGsel: {auth_text} (время сервера)\n"
            f"Проверка заказов: {checked('last_sales_check')}\n"
            f"Поиск переписок: {checked('last_chat_check')}\n"
            f"Проверка сообщений: {checked('last_message_check')}\n"
            f"Связанных переписок: {len(self._topics())}\n"
            f"Ожидают отправки в Telegram: {len(getattr(service, 'pending_messages', []))}\n"
            f"Ожидают создания темы: {len(getattr(service, 'pending_topics', []))}\n\n"
            "Время авторизации показывает последний успешный вход, а не результат проверки сети сейчас. "
            "При остановке синхронизации новые заказы, сообщения и ответы через бот не обрабатываются."
        )
        active = service.sync_enabled
        keyboard = [[self.button("⏸ Остановить" if active else "▶️ Запустить",
                                 "panel:pause" if active else "panel:resume")],
                    [self.button("🔄 Обновить", "panel:status"), self.button("◀️ В меню", "panel:main")]]
        await self._render(update, text, keyboard)

    async def _notifications(self, update):
        enabled = self.notifications_enabled()
        text = (
            "🔔 Уведомления о сообщениях\n\n"
            f"Новые сообщения покупателей: {'включены' if enabled else 'выключены'}\n"
            "Сообщения поступают в отдельные темы покупателей. Этот переключатель управляет "
            "новыми сообщениями; уведомления о заказах и отзывах работают при включённой синхронизации.\n\n"
            "Пропущенные при выключенных уведомлениях сообщения можно открыть через /history в теме.\n\n"
            "Если сообщения видны в группе, но телефон молчит, проверьте звук и уведомления "
            "самой группы и её тем в Telegram."
        )
        keyboard = [[self.button("🔕 Выключить сообщения" if enabled else "🔔 Включить сообщения",
                                 "panel:notify_toggle", "danger" if enabled else "success")],
                    [self.button("🩺 Состояние синхронизации", "panel:status")],
                    [self.button("◀️ В меню", "panel:main")]]
        await self._render(update, text, keyboard)

    async def _plugins(self, update, page):
        plugins = self.plugins.list_plugins()
        page = max(0, min(page, max(0, (len(plugins) - 1) // self.PAGE_SIZE)))
        text = ("🧩 Плагины GGsel\n\nПлагины добавляют обработчики команд покупателей. "
                "Обычные команды из раздела «Команды покупателей» имеют приоритет.\n"
                "Плагины Cardinal требуют адаптации под GGsel; напрямую они не совместимы.\n\n")
        keyboard = []
        for plugin in plugins[page * self.PAGE_SIZE:(page + 1) * self.PAGE_SIZE]:
            enabled = bool(plugin["enabled"])
            label = str(plugin.get("name") or plugin["id"])[:42]
            text += f"{'✅' if enabled else '⏸'} {label} · {plugin.get('version', '')}\n"
            if plugin.get("error"):
                text += f"⚠️ {str(plugin['error'])[:150]}\n"
            if PluginManager.ID_PATTERN.fullmatch(str(plugin["id"])):
                keyboard.append([self.button(f"{'Выключить' if enabled else 'Включить'}: {label}",
                                             f"panel:plugin:{plugin['id']}",
                                             "danger" if enabled else "success")])
        if not plugins:
            text += "Плагины пока не установлены."
        nav = []
        if page:
            nav.append(self.button("◀️", f"panel:plugins:{page - 1}"))
        if (page + 1) * self.PAGE_SIZE < len(plugins):
            nav.append(self.button("▶️", f"panel:plugins:{page + 1}"))
        if nav:
            keyboard.append(nav)
        keyboard += [[self.button("🔄 Обновить", f"panel:plugins:{page}")],
                     [self.button("◀️ В меню", "panel:main")]]
        await self._render(update, text, keyboard)

    async def _configuration(self, update):
        service = self.service
        config = service.config
        text = (
            "🗂 Конфигурация\n\n"
            f"Продавец GGsel: {config.ggsel_seller_id}\n"
            f"Группа Telegram: {config.telegram_group_id}\n"
            f"Операторов: {len(config.telegram_allowed_user_ids)}\n"
            f"Автоответы: {'включены' if service.autoresponder.is_enabled() else 'выключены'}\n"
            f"Команд и триггеров: {len(service.autoresponder.get_triggers())}\n\n"
            "Экспорт содержит тексты и настройки автоответов, а также переключатель уведомлений. "
            "Токены доступа, ключи API и переписки в экспорт не входят. Импорт заменяет эти настройки "
            "после просмотра и подтверждения. Плагины настраиваются отдельно."
        )
        await self._render(update, text, [
            [self.button("📥 Скачать JSON", "panel:config_export"), self.button("📤 Загрузить JSON", "panel:config_import")],
            [self.button("⚙️ Изменить автоответы", "auto_menu")],
            [self.button("◀️ В меню", "panel:main")],
        ])

    def export_config(self):
        # Explicit allowlist, never serialize Config or the process environment.
        auto = self.service.autoresponder.config
        allowed = {key: copy.deepcopy(auto.get(key, default))
                   for key, default in AutoResponder.DEFAULT_CONFIG.items()}
        payload = {"format": self.EXPORT_FORMAT, "version": 1,
                   "autoresponder": allowed,
                   "telegram_notifications_enabled": self.notifications_enabled()}
        validated = self.validate_config(payload)
        if len(self._encode_config(validated)) > self.MAX_CONFIG_BYTES:
            raise ValueError("Конфигурация превышает 256 КБ. Сократите тексты или число правил для экспорта")
        return validated

    @staticmethod
    def _encode_config(payload):
        return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    @classmethod
    def validate_config(cls, payload):
        def obj(value, allowed, name):
            if not isinstance(value, dict) or set(value) - set(allowed):
                raise ValueError(f"Недопустимые поля: {name}")
        def boolean(value, name):
            if type(value) is not bool:
                raise ValueError(f"Поле {name} должно быть true/false")
            return value
        def string(value, name, maximum=4000):
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"Некорректный текст: {name}, максимум {maximum} символов")
            if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
                raise ValueError(f"Управляющие символы: {name}")
            return value
        obj(payload, ("format", "version", "autoresponder", "telegram_notifications_enabled"), "файл")
        if payload.get("format") != cls.EXPORT_FORMAT or type(payload.get("version")) is not int or payload["version"] != 1:
            raise ValueError("Нужен экспорт GGsel helper версии 1")
        source = payload.get("autoresponder")
        obj(source, AutoResponder.DEFAULT_CONFIG, "autoresponder")
        # Missing switches always default OFF on import, not to upstream's ON defaults.
        auto = copy.deepcopy(AutoResponder.DEFAULT_CONFIG)
        auto["enabled"] = boolean(source.get("enabled", False), "enabled")
        auto["first_message_enabled"] = boolean(source.get("first_message_enabled", False), "first_message_enabled")
        for key in ("first_message_text", "notify_text"):
            auto[key] = string(source.get(key, auto[key]), key)
        triggers = source.get("triggers", [])
        if not isinstance(triggers, list) or len(triggers) > 100:
            raise ValueError("Разрешено не более 100 команд")
        auto["triggers"] = []
        for item in triggers:
            obj(item, AutoResponder.TRIGGER_FIELDS, "команда")
            phrase = string(item.get("phrase"), "phrase", 500).strip()
            if not phrase:
                raise ValueError("Пустая команда")
            result = {"phrase": phrase.lower(), "response": string(item.get("response", ""), "response"),
                      "notify_text": string(item.get("notify_text", ""), "notify_text")}
            for key, default in (("enabled", True), ("notify_group", False), ("exact_match", False)):
                result[key] = boolean(item.get(key, default), key)
            auto["triggers"].append(result)
        reviews = source.get("review_responses", {})
        obj(reviews, auto["review_responses"], "review_responses")
        for key, default in auto["review_responses"].items():
            auto["review_responses"][key] = (boolean(reviews.get(key, False), key) if isinstance(default, bool)
                                            else string(reviews.get(key, default), key))
        csv = source.get("csv_mode", {})
        obj(csv, ("enabled", "rules"), "csv_mode")
        auto["csv_mode"]["enabled"] = boolean(csv.get("enabled", False), "csv_mode.enabled")
        rules = csv.get("rules", [])
        if not isinstance(rules, list) or len(rules) > 100:
            raise ValueError("Разрешено не более 100 правил заказа")
        auto["csv_mode"]["rules"] = []
        for item in rules:
            obj(item, AutoResponder.CSV_RULE_FIELDS, "правило заказа")
            result = {}
            for key in ("option_name", "option_value", "user_message", "topic_message"):
                result[key] = string(item.get(key, ""), key, 500 if key.startswith("option_") else 4000)
            if not result["option_name"].strip():
                raise ValueError("Пустое название опции")
            for key, default in (("enabled", True), ("case_sensitive", False), ("send_to_user", False), ("send_to_topic", True)):
                result[key] = boolean(item.get(key, default), key)
            result["match_type"] = item.get("match_type", "name")
            if result["match_type"] not in ("name", "value", "contains"):
                raise ValueError("Неизвестный режим совпадения")
            auto["csv_mode"]["rules"].append(result)
        return {"format": cls.EXPORT_FORMAT, "version": 1, "autoresponder": auto,
                "telegram_notifications_enabled": boolean(payload.get("telegram_notifications_enabled", True), "telegram_notifications_enabled")}

    @staticmethod
    def _atomic_write(path, data):
        path = Path(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
                temporary = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def _apply_config(self, payload):
        validated = self.validate_config(payload)
        responder = self.service.autoresponder
        path = Path(responder.config_file)
        previous = path.read_bytes() if path.exists() else None
        data = (json.dumps(validated["autoresponder"], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if previous is not None:
            self._atomic_write(path.with_name(path.name + ".before-import"), previous)
        self._atomic_write(path, data)
        try:
            self.service.database.set_setting("telegram_notifications_enabled",
                                              "true" if validated["telegram_notifications_enabled"] else "false")
        except Exception:
            if previous is not None:
                self._atomic_write(path, previous)
            else:
                path.unlink(missing_ok=True)
            raise
        responder.config = validated["autoresponder"]

    async def consume_document(self, update, context):
        if not self._authorized(update):
            return False
        identity = self._identity(update)
        state = self._imports.get(identity)
        if not state or state["expires"] <= time.monotonic():
            self._imports.pop(identity, None)
            return False
        message = update.effective_message
        document = getattr(message, "document", None)
        if not document:
            return False
        if state.get("payload"):
            await message.reply_text("Сначала подтвердите или отмените предыдущий импорт.")
            return True
        if not (document.file_name or "").lower().endswith(".json") or not document.file_size or document.file_size > self.MAX_CONFIG_BYTES:
            await message.reply_text("Нужен JSON-файл размером до 256 КБ.")
            return True
        try:
            file = await document.get_file()
            raw = await file.download_as_bytearray()
            if len(raw) > self.MAX_CONFIG_BYTES:
                raise ValueError("Файл превышает 256 КБ")
            payload = self.validate_config(json.loads(raw.decode("utf-8-sig")))
        except (ValueError, UnicodeError, RecursionError) as exc:
            await message.reply_text(f"❌ Импорт отклонён: {str(exc)[:200]}")
            return True
        except Exception:
            await message.reply_text("❌ Не удалось загрузить файл из Telegram. Повторите загрузку.")
            return True
        nonce = secrets.token_hex(8)
        state.update(payload=payload, nonce=nonce, expires=time.monotonic() + self.INPUT_TTL)
        auto = payload["autoresponder"]
        text = ("📤 Предпросмотр импорта\n\n"
                f"Команд: {len(auto['triggers'])}\n"
                f"Автоответы: {'ВКЛ' if auto['enabled'] else 'выключены'}\n"
                f"Приветствие: {'ВКЛ' if auto['first_message_enabled'] else 'выключено'}\n"
                f"Ответы на отзывы: {'ВКЛ' if auto['review_responses']['enabled'] else 'выключены'}\n"
                f"Правила заказа: {'ВКЛ' if auto['csv_mode']['enabled'] else 'выключены'}\n"
                f"Уведомления о сообщениях: {'ВКЛ' if payload['telegram_notifications_enabled'] else 'выключены'}\n\n"
                "Применение заменит текущие настройки. Включённые автоответы начнут отправляться покупателям "
                "при новых событиях. Старая конфигурация сохранится на сервере.")
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup([
            [self.button("✅ Применить настройки", f"panel:config_apply:{nonce}")],
            [self.button("❌ Отмена", "panel:config_cancel")]]))
        return True

    async def handle_callback(self, data, update, context):
        if not isinstance(data, str) or not data.startswith("panel:"):
            return False
        if not self._authorized(update):
            return True
        action = data[6:]
        if not action.startswith("config_apply:") and action != "config_import":
            self.cancel_input(update)
        self.service.awaiting_input.pop(update.effective_chat.id, None)
        try:
            if action == "main":
                await self._render(update, *self.main_menu())
            elif action.startswith("chats:"):
                await self._chats(update, int(action.split(":", 1)[1]))
            elif action in ("notifications", "notify_toggle"):
                if action == "notify_toggle":
                    self.service.database.set_setting("telegram_notifications_enabled", "false" if self.notifications_enabled() else "true")
                await self._notifications(update)
            elif action in ("status", "pause", "resume"):
                if action == "pause":
                    await self.service.pause_sync()
                elif action == "resume":
                    await self.service.start_sync()
                await self._status(update)
            elif action.startswith("plugins:"):
                await self._plugins(update, int(action.split(":", 1)[1]))
            elif action.startswith("plugin:"):
                plugin_id = action.split(":", 1)[1]
                plugin = next((p for p in self.plugins.list_plugins() if p["id"] == plugin_id), None)
                if not plugin:
                    raise ValueError("Плагин не найден")
                self.plugins.set_enabled(plugin_id, not plugin["enabled"])
                await self._plugins(update, 0)
            elif action in ("config", "config_cancel"):
                await self._configuration(update)
            elif action == "config_export":
                data_bytes = self._encode_config(self.export_config())
                document = io.BytesIO(data_bytes)
                try:
                    await self.service.telegram_bot.bot.send_document(
                        chat_id=update.effective_chat.id, document=document, filename="ggsel-settings.json",
                        message_thread_id=getattr(update.effective_message, "message_thread_id", None),
                        caption="Настройки GGsel: автоответы и уведомления. Ключи доступа в файл не включены.")
                except Exception:
                    await self._render(update, "❌ Telegram не подтвердил отправку файла. Повторите экспорт позже.")
                    return True
                await self._configuration(update)
            elif action == "config_import":
                self.cancel_input(update)
                self._imports[self._identity(update)] = {"expires": time.monotonic() + self.INPUT_TTL}
                await self._render(update, "📤 Отправьте экспорт ggsel-settings.json в эту же тему. "
                                   "После проверки появится предпросмотр; настройки ещё не изменятся.",
                                   [[self.button("❌ Отмена", "panel:config_cancel")]])
            elif action.startswith("config_apply:"):
                identity = self._identity(update)
                state = self._imports.get(identity)
                nonce = action.split(":", 1)[1]
                if not state or state["expires"] <= time.monotonic() or state.get("nonce") != nonce or not state.get("payload"):
                    raise ValueError("Предпросмотр устарел. Загрузите файл ещё раз")
                self._apply_config(state["payload"])
                self._imports.pop(identity, None)
                await self._render(update, "✅ Настройки применены. Предыдущая конфигурация сохранена на сервере.",
                                   [[self.button("🗂 Конфигурация", "panel:config")]])
            elif action == "help":
                await self._render(update,
                    "❓ Как пользоваться\n\n"
                    "«Открыть панель» — интерактивная карточка в личном чате с ботом. "
                    "В группе /menu открывает полное меню настроек.\n\n"
                    "1. «Чаты и ответы» → покупатель. Напишите обычный текст в его теме: бот отправит его в GGsel. "
                    "🔥 — API принял отправку; сообщение об ошибке — ответ не подтверждён.\n\n"
                    "2. «Команды покупателей» → добавить. Например, /help и текст инструкции. "
                    "Для команды выберите точное совпадение и включите автоответы. Покупатель пишет команду в чате GGsel.\n\n"
                    "3. «Уведомления» — сообщения; «Состояние» — остановить или запустить всю синхронизацию.\n\n"
                    "4. /history в теме — история; /menu — управление; /cancel — отмена ввода настройки. "
                    "Команды управления не отправляются покупателю.\n\n"
                    "5. «Конфигурация» — сохранить и восстановить тексты и настройки. «Плагины» — включить установленные расширения.")
            else:
                await self._render(update, "Эта кнопка устарела. Откройте главное меню.")
        except (ValueError, TypeError) as exc:
            await self._render(update, f"❌ {str(exc)[:200]}")
        except OSError:
            await self._render(update, "❌ Не удалось сохранить настройки. Изменение не подтверждено; повторите попытку.")
        return True
