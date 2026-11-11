import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram.ext import ApplicationHandlerStop

from config import Config
from telegram_bot import TelegramBot


class EditorRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = TelegramBot(Config(
            ggsel_seller_id=1, ggsel_api_key="test", telegram_bot_token="123:abc",
            telegram_group_id=-1001, telegram_allowed_user_ids=frozenset({42, 43}),
        ))
        self.panel = SimpleNamespace(
            service=SimpleNamespace(awaiting_input={-1001: {"type": "trigger_phrase"}}),
            has_pending_input=Mock(return_value=False), cancel_input=Mock(),
            consume_document=AsyncMock(return_value=True),
        )
        self.bot.set_operator_panel(self.panel)
        self.bot.general_message_handler = AsyncMock()
        self.bot.topic_message_handler = Mock()

    def update(self, text="/help", user_id=42, topic=15):
        message = SimpleNamespace(text=text, message_thread_id=topic, reply_text=AsyncMock())
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=-1001),
            effective_message=message, message=message,
        )

    async def test_buyer_slash_command_consumed_in_topic_editor(self):
        update = self.update()
        with self.assertRaises(ApplicationHandlerStop):
            await self.bot._handle_editor_input(update, None)
        self.bot.general_message_handler.assert_awaited_once_with("/help")
        self.bot.topic_message_handler.assert_not_called()

    async def test_customer_reply_unaffected_without_editor(self):
        self.panel.service.awaiting_input.clear()
        await self.bot._handle_editor_input(self.update("Здравствуйте"), None)
        self.bot.general_message_handler.assert_not_awaited()

    async def test_cancel_exits_editor_in_any_topic(self):
        update = self.update("/cancel")
        await self.bot._handle_editor_input(update, None)
        await self.bot._handle_cancel_command(update, None)
        self.assertNotIn(-1001, self.panel.service.awaiting_input)
        self.panel.cancel_input.assert_called_once_with(update)
        self.bot.general_message_handler.assert_not_awaited()

    async def test_other_operator_cannot_fill_active_wizard(self):
        self.bot._editor_user_id = 42
        with self.assertRaises(ApplicationHandlerStop):
            await self.bot._handle_editor_input(self.update("wrong", 43), None)
        self.bot.general_message_handler.assert_not_awaited()

    async def test_sync_controls_are_not_saved_as_buyer_reply(self):
        self.panel.service.awaiting_input[-1001] = {"type": "trigger_response"}
        for command in ("/stop_sync", "/start_sync", "/id"):
            await self.bot._handle_editor_input(self.update(command), None)
        self.bot.general_message_handler.assert_not_awaited()

    async def test_unauthorized_user_cannot_fill_wizard(self):
        with self.assertRaises(ApplicationHandlerStop):
            await self.bot._handle_editor_input(self.update("wrong", 99), None)
        self.bot.general_message_handler.assert_not_awaited()

    async def test_import_text_is_not_customer_reply(self):
        self.panel.service.awaiting_input.clear()
        self.panel.has_pending_input.return_value = True
        update = self.update("text while importing")
        with self.assertRaises(ApplicationHandlerStop):
            await self.bot._handle_editor_input(update, None)
        update.effective_message.reply_text.assert_awaited_once()
        self.bot.topic_message_handler.assert_not_called()

    async def test_document_requires_authorized_operator(self):
        await self.bot._handle_document(self.update(user_id=99), None)
        self.panel.consume_document.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
