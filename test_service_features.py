"""Service regressions using SQLite and mocked transports; no external writes."""
import asyncio
import gc
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from autoresponder import AutoResponder
from bot_service import BotService
from database import Database
from message_manager import MessageManager
from topic_manager import TopicManager


class ServiceFeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(gc.collect)
        self.service = service = object.__new__(BotService)
        service.database = Database(str(Path(self.tmp.name) / 'bot.db'))
        service.message_manager = MessageManager(service.database)
        service.topic_manager = TopicManager(service.database)
        service.autoresponder = AutoResponder(str(Path(self.tmp.name) / 'auto.json'))
        service.autoresponder.add_trigger('!help', 'Инструкция', exact_match=True)
        service.ggsel_api = Mock()
        service.ggsel_api.send_message.return_value = True
        service.ggsel_api.last_failure = None
        service.telegram_bot = SimpleNamespace(
            create_topic=AsyncMock(return_value=(123, None)),
            send_message=AsyncMock(return_value=(True, None)),
            add_reaction=AsyncMock(),
            send_message_with_keyboard=AsyncMock(),
            edit_message=AsyncMock(),
        )
        service.sync_enabled = True
        service.message_flood_control_until = None
        service.pending_messages = []
        service.flood_control_until = None
        service.ensure_ggsel_auth = AsyncMock(return_value=True)
        service.awaiting_input = {}
        service._started_at = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def message(self, **kwargs):
        return {'id': 17, 'message': '!help', 'buyer': 1, 'seller': 0,
                'date_written': '2026-11-10T18:00:00+03:00', **kwargs}

    async def test_seller_echo_unknown_and_deleted_never_trigger_customer_reply(self):
        for message in [self.message(id=18, buyer=0, seller=1),
                        self.message(id=19, buyer=None, seller=None),
                        self.message(id=20, buyer=1, seller=1),
                        self.message(id=21, deleted=1)]:
            await self.service.process_single_message_check(7, 123, message)
        self.service.ggsel_api.send_message.assert_not_called()
        await self.service.process_single_message_check(7, 123, self.message())
        await self.service.process_single_message_check(7, 123, self.message())
        self.service.ggsel_api.send_message.assert_called_once_with(7, 'Инструкция')

    async def test_notification_off_does_not_disable_commands_or_backfill_notifications(self):
        panel = SimpleNamespace(notifications_enabled=lambda: False, plugin_response=AsyncMock(return_value=None))
        self.service.operator_panel = panel
        await self.service.process_single_message_check(7, 123, self.message())
        self.service.telegram_bot.send_message.assert_not_awaited()
        self.service.ggsel_api.send_message.assert_called_once_with(7, 'Инструкция')
        panel.notifications_enabled = lambda: True
        await self.service.process_single_message_check(7, 123, self.message())
        self.service.telegram_bot.send_message.assert_not_awaited()

    async def test_plugin_only_receives_unmatched_buyer_messages(self):
        panel = SimpleNamespace(notifications_enabled=lambda: True,
                                plugin_response=AsyncMock(return_value={'response': 'plugin'}))
        self.service.operator_panel = panel
        await self.service.process_single_message_check(7, 123, self.message(message='!plugin'))
        await self.service.process_single_message_check(7, 123, self.message(id=18, message='!plugin', buyer=0, seller=1))
        panel.plugin_response.assert_awaited_once_with('!plugin')
        self.service.ggsel_api.send_message.assert_called_once_with(7, 'plugin')

    async def test_discovery_uses_chats_without_sales_and_reuses_topic(self):
        self.service.ggsel_api.get_chats.return_value = {'cnt_pages': 999, 'items': [
            {'id_i': None}, {'id_i': 7, 'email': None, 'cnt_new': 0}]}
        self.service.ggsel_api.get_chat_messages.return_value = []
        await self.service.discover_chats()
        await self.service.discover_chats()
        topic = self.service.topic_manager.get_all_topics()['chat_7']
        self.assertEqual(topic['invoice_id'], 7)
        self.assertEqual(topic['topic_id'], 123)
        self.service.telegram_bot.create_topic.assert_awaited_once()
        self.service.ggsel_api.get_last_sales.assert_not_called()
        self.service.ggsel_api.send_message.assert_not_called()

    async def test_invalid_placeholder_rows_stop_pagination_without_topics(self):
        self.service.ggsel_api.get_chats.return_value = {'cnt_pages': 999, 'items': [{'id_i': None}, {'id_i': -1}]}
        await self.service.discover_chats()
        self.service.ggsel_api.get_chats.assert_called_once()
        self.service.telegram_bot.create_topic.assert_not_awaited()

    async def test_manual_reply_routes_to_conversation_not_telegram_topic(self):
        self.service.topic_manager._save_single('chat_7', {'topic_id': 123, 'invoice_id': 7})
        await self.service._handle_topic_message_async(123, 'Ответ покупателю', 'operator', 55)
        self.service.ggsel_api.send_message.assert_called_once_with(7, 'Ответ покупателю')
        self.service.telegram_bot.add_reaction.assert_awaited_once_with(55, 123, '🔥')
        self.service.ggsel_api.send_message.reset_mock()
        await self.service._handle_topic_message_async(999, 'Другой текст', 'operator', 56)
        self.service.ggsel_api.send_message.assert_not_called()

    async def test_history_retry_never_triggers_or_mirrors_fake_auto_reply(self):
        msg = self.message(date_written='2026-10-01T12:00:00Z')
        self.service.ggsel_api.get_chat_messages.return_value = [msg]
        self.service.telegram_bot.send_message.side_effect = [(False, 60), (True, None)]
        with patch('bot_service.asyncio.sleep', new=AsyncMock()):
            await self.service.load_chat_history([7], 123)
        self.service.message_flood_control_until = None
        await self.service.process_single_message_check(7, 123, msg)
        self.service.ggsel_api.send_message.assert_not_called()
        self.assertEqual(self.service.telegram_bot.send_message.await_count, 2)
        self.assertTrue(self.service.message_manager.is_message_processed(7, '17'))

    async def test_empty_history_does_not_send_greeting_and_explicit_history_replays(self):
        self.service.ggsel_api.get_chat_messages.return_value = []
        await self.service.load_chat_history([7], 123)
        self.service.ggsel_api.send_message.assert_not_called()
        self.service.ggsel_api.get_chat_messages.return_value = [self.message()]
        with patch('bot_service.asyncio.sleep', new=AsyncMock()):
            await self.service.load_chat_history([7], 123)
            await self.service.load_chat_history([7], 123, force_reload=True)
        self.assertEqual(self.service.telegram_bot.send_message.await_count, 2)

    async def test_first_import_baselines_unknown_time_but_leaves_live_aware_message(self):
        naive = self.message(id=30, date_written='2026-11-10T18:00:00')
        aware = self.message(id=31, date_written='2026-11-10T18:00:00+03:00')
        self.service.ggsel_api.get_chat_messages.return_value = [naive, aware]
        with patch('bot_service.asyncio.sleep', new=AsyncMock()):
            await self.service.load_chat_history([7], 123, before=self.service._started_at)
        await self.service.process_single_message_check(7, 123, naive)
        await self.service.process_single_message_check(7, 123, aware)
        self.service.ggsel_api.send_message.assert_called_once_with(7, 'Инструкция')

    async def test_command_wizard_uses_exact_match_and_cancel_releases_input(self):
        self.service.awaiting_input[1] = {'type': 'trigger_phrase'}
        await self.service.handle_text_input(1, '/manual')
        await self.service.handle_text_input(1, 'Текст инструкции')
        self.assertTrue(self.service.autoresponder.get_triggers()[-1]['exact_match'])
        self.assertIsNone(self.service.autoresponder.find_response('/manualmore'))
        self.service.awaiting_input[1] = {'type': 'first_message'}
        update = SimpleNamespace(callback_query=SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=1),message_id=9)))
        await self.service.handle_callback('auto_triggers', update, None)
        self.assertNotIn(1, self.service.awaiting_input)

    async def test_old_add_command_button_cancels_pending_config_import(self):
        panel = SimpleNamespace(handle_callback=AsyncMock(return_value=False), cancel_input=Mock())
        self.service.operator_panel = panel
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1),
            callback_query=SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=9)),
        )
        await self.service.handle_callback('auto_add_trigger', update, None)
        panel.cancel_input.assert_called_once_with(update)
        self.assertEqual(self.service.awaiting_input[1]['type'], 'trigger_phrase')


if __name__ == '__main__':
    unittest.main()
