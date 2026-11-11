import asyncio
import copy
import gc
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import telegram
from telegram.error import TimedOut

import operator_panel
from autoresponder import AutoResponder
from database import Database
from operator_panel import OperatorPanel


class OperatorPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        root = Path(self.temp.name)
        self.database = Database(str(root / "state.db"))
        config = SimpleNamespace(telegram_group_id=-1001234567890, telegram_allowed_user_ids={42, 43}, ggsel_seller_id=123)
        responder = AutoResponder(str(root / "autoresponder.json"))
        responder.config["enabled"] = False
        responder.config["first_message_enabled"] = False
        responder.save_config()
        self.service = SimpleNamespace(
            database=self.database, config=config, sync_enabled=True, last_auth_time=None,
            topic_manager=SimpleNamespace(get_all_topics=Mock(return_value={})),
            autoresponder=responder, awaiting_input={},
            telegram_bot=SimpleNamespace(edit_message=AsyncMock(), bot=SimpleNamespace(send_document=AsyncMock())),
            pause_sync=AsyncMock(), start_sync=AsyncMock(), pending_messages=[], pending_topics=[])
        # Older focused tests temporarily stub Telegram while importing services.
        # Use actual PTB values for these UI contract assertions.
        self.buttons = patch.object(operator_panel, "InlineKeyboardButton", telegram.InlineKeyboardButton)
        self.markup = patch.object(operator_panel, "InlineKeyboardMarkup", telegram.InlineKeyboardMarkup)
        self.buttons.start(); self.markup.start()
        self.addCleanup(self.buttons.stop); self.addCleanup(self.markup.stop)
        self.panel = OperatorPanel(self.service)

    def update(self, actor=42, thread=1, document=None):
        message = SimpleNamespace(message_id=10, message_thread_id=thread,
                                  chat=SimpleNamespace(id=-1001234567890),
                                  reply_text=AsyncMock(), document=document)
        return SimpleNamespace(effective_user=SimpleNamespace(id=actor), effective_chat=message.chat,
                               effective_message=message, message=message,
                               callback_query=SimpleNamespace(message=message))

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def payload(self):
        return self.panel.export_config()

    def test_main_menu_exposes_workflows_and_valid_callback_lengths(self):
        text, keyboard = self.panel.main_menu()
        callbacks = {b.callback_data for row in keyboard for b in row}
        self.assertTrue({"panel:chats:0", "panel:notifications", "auto_triggers", "panel:config", "panel:plugins:0"} <= callbacks)
        self.assertIn("синхронизация", text.lower())
        self.assertTrue(all(len(value.encode()) <= 64 for value in callbacks))

    def test_chats_paginate_and_use_topic_links(self):
        self.service.topic_manager.get_all_topics.return_value = {
            str(i): {"invoice_id": i, "topic_id": 100 + i, "created_at": f"2026-01-{i:02}", "topic_name": f"Buyer{i}"}
            for i in range(1, 12)}
        self.run_async(self.panel.handle_callback("panel:chats:0", self.update(), None))
        keyboard = self.service.telegram_bot.edit_message.await_args.args[3]
        links = [b.url for row in keyboard for b in row if b.url]
        self.assertEqual(len(links), 8)
        self.assertEqual(links[0], "https://t.me/c/1234567890/111")
        self.assertIn("panel:chats:1", [b.callback_data for row in keyboard for b in row])

    def test_notification_setting_survives_panel_restart(self):
        self.assertTrue(self.panel.notifications_enabled())
        self.run_async(self.panel.handle_callback("panel:notify_toggle", self.update(), None))
        self.assertFalse(OperatorPanel(self.service).notifications_enabled())

    def test_unauthorized_callback_cannot_change_settings(self):
        self.run_async(self.panel.handle_callback("panel:notify_toggle", self.update(actor=99), None))
        self.assertTrue(self.panel.notifications_enabled())
        self.service.telegram_bot.edit_message.assert_not_awaited()

    def test_export_does_not_include_credentials_or_unknown_top_level_fields(self):
        self.service.config.ggsel_api_key = "secret-ggsel"
        self.service.config.telegram_bot_token = "secret-telegram"
        self.service.autoresponder.config["api_key"] = "unwanted"
        encoded = json.dumps(self.panel.export_config())
        self.assertNotIn("secret", encoded)
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("unwanted", encoded)

    def test_import_rejects_wrong_types_unknown_fields_and_oversized_rules(self):
        for value in ("false", 1, None):
            payload = self.payload()
            payload["autoresponder"]["enabled"] = value
            with self.assertRaises(ValueError):
                self.panel.validate_config(payload)
        payload = self.payload()
        payload["autoresponder"]["triggers"] = [{"phrase": "/help", "response": "x", "execute": "oops"}]
        with self.assertRaises(ValueError):
            self.panel.validate_config(payload)
        payload["autoresponder"]["triggers"] = [{"phrase": "/help", "response": "x"}] * 101
        with self.assertRaises(ValueError):
            self.panel.validate_config(payload)

    def test_missing_automation_switches_default_off(self):
        payload = {"format": self.panel.EXPORT_FORMAT, "version": 1, "autoresponder": {}}
        actual = self.panel.validate_config(payload)["autoresponder"]
        self.assertFalse(actual["enabled"])
        self.assertFalse(actual["first_message_enabled"])
        self.assertFalse(actual["review_responses"]["enabled"])

    def test_export_rejects_file_too_large_to_import(self):
        self.service.autoresponder.config["triggers"] = [
            {"phrase": f"/command{i}", "response": "text" * 1000} for i in range(70)]
        with self.assertRaisesRegex(ValueError, "256"):
            self.panel.export_config()
        self.run_async(self.panel.handle_callback("panel:config_export", self.update(), None))
        self.service.telegram_bot.bot.send_document.assert_not_awaited()

    def test_export_network_failure_reports_no_success(self):
        self.service.telegram_bot.bot.send_document.side_effect = TimedOut()
        self.run_async(self.panel.handle_callback("panel:config_export", self.update(), None))
        self.assertIn("не подтвердил", self.service.telegram_bot.edit_message.await_args.args[2])

    def document(self, payload, **kwargs):
        data = json.dumps(payload).encode()
        file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(data)))
        values = {"file_name": "settings.json", "file_size": len(data), "get_file": AsyncMock(return_value=file)}
        values.update(kwargs)
        return SimpleNamespace(**values)

    def test_import_requires_same_operator_thread_and_explicit_confirmation(self):
        update = self.update()
        self.run_async(self.panel.handle_callback("panel:config_import", update, None))
        payload = self.payload()
        payload["autoresponder"]["enabled"] = True
        document = self.document(payload)
        self.assertFalse(self.run_async(self.panel.consume_document(self.update(actor=43, document=document), None)))
        self.assertFalse(self.run_async(self.panel.consume_document(self.update(thread=2, document=document), None)))
        upload = self.update(document=document)
        self.assertTrue(self.run_async(self.panel.consume_document(upload, None)))
        self.assertFalse(self.service.autoresponder.is_enabled())
        markup = upload.effective_message.reply_text.await_args.kwargs["reply_markup"]
        confirm = markup.inline_keyboard[0][0].callback_data
        self.run_async(self.panel.handle_callback(confirm, update, None))
        self.assertTrue(self.service.autoresponder.is_enabled())
        self.assertTrue(Path(self.service.autoresponder.config_file + ".before-import").exists())
        # A used button cannot apply the old import a second time.
        self.service.autoresponder.config["enabled"] = False
        self.run_async(self.panel.handle_callback(confirm, update, None))
        self.assertFalse(self.service.autoresponder.is_enabled())

    def test_cancel_prevents_later_document_import(self):
        update = self.update()
        self.run_async(self.panel.handle_callback("panel:config_import", update, None))
        self.panel.cancel_input(update)
        document = self.document(self.payload())
        self.assertFalse(self.run_async(self.panel.consume_document(self.update(document=document), None)))
        document.get_file.assert_not_awaited()

    def test_cancel_in_other_topic_clears_actor_imports_only(self):
        self.run_async(self.panel.handle_callback("panel:config_import", self.update(actor=42, thread=1), None))
        self.run_async(self.panel.handle_callback("panel:config_import", self.update(actor=43, thread=2), None))
        self.panel.cancel_input(self.update(actor=42, thread=3))
        self.assertFalse(self.panel.has_pending_input(self.update(actor=42, thread=1)))
        self.assertTrue(self.panel.has_pending_input(self.update(actor=43, thread=2)))

    def test_large_document_is_not_downloaded(self):
        update = self.update()
        self.run_async(self.panel.handle_callback("panel:config_import", update, None))
        document = self.document(self.payload(), file_size=self.panel.MAX_CONFIG_BYTES + 1)
        self.assertTrue(self.run_async(self.panel.consume_document(self.update(document=document), None)))
        document.get_file.assert_not_awaited()

    def test_failed_database_apply_restores_file_and_memory(self):
        path = Path(self.service.autoresponder.config_file)
        original = path.read_bytes()
        payload = self.payload()
        payload["autoresponder"]["enabled"] = True
        with patch.object(self.database, "set_setting", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.panel._apply_config(payload)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.service.autoresponder.is_enabled())

    def test_plugin_response_delegates_without_customer_api(self):
        self.panel.plugins = SimpleNamespace(find_response=AsyncMock(return_value={"response": "result"}))
        self.assertEqual(self.run_async(self.panel.plugin_response("/help")), {"response": "result"})
        self.panel.plugins.find_response.assert_awaited_once_with("/help")


if __name__ == "__main__":
    unittest.main()
