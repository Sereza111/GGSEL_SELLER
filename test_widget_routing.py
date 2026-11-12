"""Integration boundaries between PTB routing and the widget dashboard."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram import Update
from telegram.ext import ApplicationHandlerStop, TypeHandler
from telegram_bot import OperatorUpdateProcessor, TelegramBot
from test_telegram_auth import make_config, make_update


class WidgetRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_widget_processing_does_not_block_behind_ordered_messages(self):
        processor = OperatorUpdateProcessor()
        await processor.initialize()
        entered, release, answered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []

        async def first_message():
            order.append('first')
            entered.set()
            await release.wait()
            order.append('first-done')

        async def second_message():
            order.append('second')

        async def widget():
            answered.set()

        normal = Update.de_json({'update_id': 1}, None)
        raw_widget = Update.de_json({'update_id': 2, 'widget_query': {'id': 'q'}}, None)
        first = asyncio.create_task(processor.process_update(normal, first_message()))
        await entered.wait()
        waiting = [asyncio.create_task(processor.process_update(normal, second_message())) for _ in range(40)]
        third = asyncio.create_task(processor.process_update(raw_widget, widget()))
        try:
            await asyncio.wait_for(answered.wait(), timeout=1)
            self.assertEqual(order, ['first'])
        finally:
            release.set()
            await asyncio.gather(first, *waiting, third)
            await processor.shutdown()
        self.assertEqual(order, ['first', 'first-done'] + ['second'] * 40)

    async def test_processor_shutdown_after_worker_cancellation_closes_pending_messages(self):
        processor = OperatorUpdateProcessor()
        await processor.initialize()
        entered = asyncio.Event()

        async def blocking():
            entered.set()
            await asyncio.Event().wait()

        await processor.process_update(Update.de_json({'update_id': 1}, None), blocking())
        await entered.wait()
        queued = blocking()
        await processor.process_update(Update.de_json({'update_id': 2}, None), queued)
        processor._worker.cancel()
        await asyncio.gather(processor._worker, return_exceptions=True)
        await asyncio.wait_for(processor.shutdown(), 1)
        self.assertIsNone(queued.cr_frame)

    async def test_raw_widget_is_consumed_before_group_handlers(self):
        bot = TelegramBot(make_config())
        bot.set_widget_dashboard(SimpleNamespace(handle_update=AsyncMock(return_value=True)))
        update = Update.de_json({'update_id': 1, 'widget_query': {'id': 'q'}}, None)
        with self.assertRaises(ApplicationHandlerStop):
            await bot._handle_widget_update(update, None)
        bot.widget_dashboard.handle_update.assert_awaited_once_with(update, None)
        bot.widget_dashboard.handle_update.return_value = False
        await bot._handle_widget_update(make_update(), None)

    async def test_private_menu_delegates_and_group_menu_still_authorizes(self):
        bot = TelegramBot(make_config())
        dashboard = SimpleNamespace(open=AsyncMock(return_value=True))
        bot.set_widget_dashboard(dashboard)
        private = make_update(chat_id=42)
        private.callback_query = None
        await bot._handle_menu_command(private, None)
        dashboard.open.assert_awaited_once_with(private)
        private.effective_message.reply_text.assert_not_awaited()
        dashboard.open.return_value = False
        unauthorized = make_update(user_id=99)
        await bot._handle_menu_command(unauthorized, None)
        unauthorized.effective_message.reply_text.assert_not_awaited()

    async def test_polling_requests_widget_updates_and_installs_processor(self):
        bot = TelegramBot(make_config())
        bot.set_widget_dashboard(SimpleNamespace(open=AsyncMock(), handle_update=AsyncMock()))
        bot.bot = SimpleNamespace(username='example_bot')
        builder = Mock()
        builder.bot.return_value = builder
        builder.concurrent_updates.return_value = builder
        application = SimpleNamespace(add_handler=Mock(), initialize=AsyncMock(),
            start=AsyncMock(), updater=SimpleNamespace(start_polling=AsyncMock()))
        builder.build.return_value = application
        with patch('telegram_bot.Application', SimpleNamespace(builder=lambda: builder)):
            await bot.start()
        self.assertIsInstance(builder.concurrent_updates.call_args.args[0], OperatorUpdateProcessor)
        handlers = application.add_handler.call_args_list
        first = handlers[0]
        self.assertIsInstance(first.args[0], TypeHandler)
        self.assertEqual(first.kwargs, {'group': -2})
        application.updater.start_polling.assert_awaited_once_with(
            allowed_updates=['message', 'callback_query', 'widget_query'])
        self.assertEqual(bot.public_username, 'example_bot')
