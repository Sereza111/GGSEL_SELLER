import asyncio
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram import Update
from telegram.error import BadRequest, Forbidden

from widget_dashboard import WidgetDashboard


class MemorySettings:
    def __init__(self):
        self.values = {}

    def get_setting(self, key):
        return self.values.get(key)

    def set_setting(self, key, value):
        self.values[key] = value


class WidgetDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.query_number = 0
        self.database = MemorySettings()
        self.transport = AsyncMock(return_value={"message_id": 123})
        self.service = SimpleNamespace(
            config=SimpleNamespace(telegram_allowed_user_ids={42, 43}, telegram_group_id=-1001234567890),
            telegram_bot=SimpleNamespace(bot=SimpleNamespace(do_api_request=self.transport, send_message=AsyncMock())),
            database=self.database,
            topic_manager=SimpleNamespace(get_all_topics=Mock(return_value={})),
            autoresponder=SimpleNamespace(get_triggers=lambda: []),
            operator_panel=SimpleNamespace(
                notifications_enabled=lambda: self.database.get_setting('telegram_notifications_enabled') != 'false',
                plugins=SimpleNamespace(list_plugins=lambda: [])),
            sync_enabled=True, running=True, pending_messages=[],
            pause_sync=AsyncMock(), start_sync=AsyncMock(),
            last_sales_check=datetime.now(timezone.utc), last_chat_check=None, last_message_check=None,
        )
        self.dashboard = WidgetDashboard(self.service)
        self.source = patch.object(WidgetDashboard, '_html', return_value=('<html>test</html>', 'layout1'))
        self.source.start()
        self.addCleanup(self.source.stop)

    def update(self, actor=42, chat_type='private', chat_id=None):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(type=chat_type, id=actor if chat_id is None else chat_id),
            effective_user=SimpleNamespace(id=actor), effective_message=SimpleNamespace(reply_text=AsyncMock()))

    def query(self, action='refresh', actor=42, message=123, **overrides):
        self.query_number += 1
        raw = {'id': f'query-{self.query_number}', 'from': {'id': actor, 'is_bot': False, 'first_name': 'Operator'},
               'chat': {'id': actor, 'type': 'private'}, 'message_id': message,
               'type': {'type': 'data'}, 'data': action, **overrides}
        return Update.de_json({'update_id': 1, 'widget_query': raw}, None)

    def register(self, actor=42):
        self.dashboard._save(actor, {'message_id': 123, 'layout': 'layout1', 'view': 'home', 'page': 0})

    async def test_group_and_unlisted_users_cannot_open_dashboard(self):
        self.assertFalse(await self.dashboard.open(self.update(chat_type='supergroup', chat_id=-1001234567890)))
        denied = self.update(actor=99)
        self.assertTrue(await self.dashboard.open(denied))
        denied.effective_message.reply_text.assert_awaited_once()
        self.transport.assert_not_awaited()

    async def test_open_sends_private_state_and_reuses_persisted_message_after_restart(self):
        await self.dashboard.open(self.update())
        request = self.transport.await_args.kwargs['api_kwargs']
        self.assertEqual(self.transport.await_args.args[0], 'sendWidget')
        self.assertEqual(request['chat_id'], 42)
        self.assertTrue(request['allow_queries'])
        self.assertNotIn('public_data', request)
        self.assertNotIn('is_shared', request)
        self.assertEqual(self.dashboard._saved(42)['message_id'], 123)
        self.transport.reset_mock()
        restarted = WidgetDashboard(self.service)
        await restarted.open(self.update())
        self.assertEqual(self.transport.await_args.args[0], 'editMessageWidgetData')

    async def test_new_layout_sends_new_widget_and_rejects_old_queries(self):
        self.register()
        with patch.object(WidgetDashboard, '_html', return_value=('<html>new</html>', 'layout2')):
            self.transport.return_value = {'message_id': 456}
            await self.dashboard.open(self.update())
        self.assertEqual(self.dashboard._saved(42)['message_id'], 456)
        self.transport.reset_mock()
        await self.dashboard.handle_update(self.query(message=123))
        self.assertEqual(self.transport.await_count, 1)
        self.assertIn('error', json.loads(self.transport.await_args.kwargs['api_kwargs']['data']))

    async def test_unavailable_widget_falls_back_but_permission_errors_propagate(self):
        self.transport.side_effect = BadRequest('method unavailable')
        await self.dashboard.open(self.update())
        self.service.telegram_bot.bot.send_message.assert_awaited_once()
        self.assertIsNone(self.dashboard._saved(42))
        self.transport.side_effect = Forbidden('blocked')
        with self.assertRaises(Forbidden):
            await self.dashboard.open(self.update())

    async def test_forged_contexts_cannot_leak_state_or_toggle_settings(self):
        self.register()
        for update in [self.query('notify:off', actor=99), self.query('notify:off', message=999),
                       self.query('notify:off', chat={'id': -1001234567890, 'type': 'supergroup'}),
                       self.query('notify:off', type={'type': 'sign_data'}),
                       self.query('notify:off', widget_id='shared'), self.query('x' * 65)]:
            self.transport.reset_mock()
            await self.dashboard.handle_update(update)
            self.assertEqual(self.transport.await_count, 1)
            data = json.loads(self.transport.await_args.kwargs['api_kwargs']['data'])
            self.assertEqual(set(data), {'error'})
            self.assertIsNone(self.database.get_setting('telegram_notifications_enabled'))

    async def test_notification_setting_is_explicit_and_idempotent(self):
        self.register()
        for _ in range(2):
            await self.dashboard.handle_update(self.query('notify:off'))
        self.assertEqual(self.database.get_setting('telegram_notifications_enabled'), 'false')
        await self.dashboard.handle_update(self.query('notify:on'))
        self.assertEqual(self.database.get_setting('telegram_notifications_enabled'), 'true')
        self.service.pause_sync.assert_not_awaited()

    async def test_redelivered_query_cannot_override_a_later_setting(self):
        self.register()
        earlier = self.query('notify:off')
        await self.dashboard.handle_update(earlier)
        await self.dashboard.handle_update(self.query('notify:on'))
        self.transport.reset_mock()
        await self.dashboard.handle_update(earlier)
        self.assertEqual(self.database.get_setting('telegram_notifications_enabled'), 'true')
        self.assertEqual(self.transport.await_count, 1)
        self.assertIn('error', json.loads(self.transport.await_args.kwargs['api_kwargs']['data']))

    async def test_query_history_is_bounded_and_expires(self):
        for number in range(self.dashboard.QUERY_HISTORY_LIMIT + 10):
            self.assertTrue(self.dashboard._remember_query(str(number)))
        self.assertEqual(len(self.dashboard._queries), self.dashboard.QUERY_HISTORY_LIMIT)
        self.assertFalse(self.dashboard._remember_query(str(number)))
        with patch('widget_dashboard.time.monotonic', return_value=time.monotonic() + 301):
            self.assertTrue(self.dashboard._remember_query(str(number)))
        self.assertEqual(len(self.dashboard._queries), 1)

    async def test_locked_widget_answers_immediately_without_mutation(self):
        self.register()
        lock = self.dashboard._locks.setdefault(42, asyncio.Lock())
        async with lock:
            await asyncio.wait_for(self.dashboard.handle_update(self.query('notify:off')), timeout=1)
        self.assertIsNone(self.database.get_setting('telegram_notifications_enabled'))
        self.assertEqual(self.transport.await_args.args[0], 'answerWidgetQuery')

    async def test_request_has_total_timeout_below_widget_deadline(self):
        original = asyncio.wait_for
        with patch('widget_dashboard.asyncio.wait_for', wraps=original) as wait:
            await self.dashboard._answer('query', '{}')
        self.assertLess(wait.call_args.kwargs['timeout'], 15)

    async def test_pause_requires_fresh_single_use_confirmation_and_ack_precedes_pause(self):
        self.register()
        await self.dashboard.handle_update(self.query('sync:pause'))
        nonce = self.dashboard._confirmations[42]['nonce']
        self.service.pause_sync.assert_not_awaited()
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause():
            self.assertTrue(any(c.args[0] == 'answerWidgetQuery' for c in self.transport.await_args_list))
            entered.set()
            await release.wait()
            self.service.sync_enabled = False

        self.service.pause_sync.side_effect = pause
        self.transport.reset_mock()
        await self.dashboard.handle_update(self.query('confirm:' + nonce))
        self.assertEqual(self.transport.await_args_list[0].args[0], 'answerWidgetQuery')
        await entered.wait()
        task = self.dashboard._transition
        await self.dashboard.handle_update(self.query('confirm:' + nonce))
        release.set()
        await task
        self.service.pause_sync.assert_awaited_once()
        self.assertIsNone(self.dashboard._transition)
        last = self.transport.await_args.kwargs['api_kwargs']
        state = json.loads(last['chat_data'])
        self.assertFalse(state['busy'])
        self.assertFalse(state['sync'])

    async def test_confirmation_is_bound_to_actor_and_expiry(self):
        self.register(); self.register(43)
        await self.dashboard.handle_update(self.query('sync:pause'))
        nonce = self.dashboard._confirmations[42]['nonce']
        await self.dashboard.handle_update(self.query('confirm:' + nonce, actor=43))
        self.service.pause_sync.assert_not_awaited()
        self.dashboard._confirmations[42]['expires'] = time.monotonic() - 1
        await self.dashboard.handle_update(self.query('confirm:' + nonce))
        self.service.pause_sync.assert_not_awaited()

    async def test_transition_completion_preserves_latest_view(self):
        self.register()
        entered, release = asyncio.Event(), asyncio.Event()

        async def resume():
            entered.set()
            await release.wait()

        self.service.start_sync.side_effect = resume
        await self.dashboard.handle_update(self.query('sync:resume'))
        await entered.wait()
        task = self.dashboard._transition
        await self.dashboard.handle_update(self.query('view:settings'))
        release.set()
        await task
        state = json.loads(self.transport.await_args.kwargs['api_kwargs']['chat_data'])
        self.assertEqual(state['view'], 'settings')
        self.assertFalse(state['busy'])

    async def test_cancelled_transition_does_not_publish_during_shutdown(self):
        self.register()
        entered = asyncio.Event()

        async def resume():
            entered.set()
            await asyncio.Event().wait()

        self.service.start_sync.side_effect = resume
        await self.dashboard.handle_update(self.query('sync:resume'))
        await entered.wait()
        task = self.dashboard._transition
        self.transport.reset_mock()
        await self.dashboard.close()
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.dashboard._transition)
        self.transport.assert_not_awaited()
        self.assertFalse(self.dashboard._transition_tasks)

    async def test_unknown_action_does_not_call_service(self):
        self.register()
        await self.dashboard.handle_update(self.query('send:buyer:hello'))
        self.assertIn('error', json.loads(self.transport.await_args.kwargs['api_kwargs']['data']))
        self.service.pause_sync.assert_not_awaited()
        self.service.start_sync.assert_not_awaited()

    async def test_state_is_bounded_and_contains_no_customer_content_or_credentials(self):
        self.service.topic_manager.get_all_topics.return_value = {
            str(i): {'invoice_id': 9999999999999999-i, 'topic_id': 9999999999999999-i,
                     'email': 'secret@example.com', 'topic_name': 'private name', 'content': 'private message'}
            for i in range(30)}
        self.dashboard._notice = 'Не удалось изменить состояние. Проверьте меню группы.'
        self.dashboard._confirmations[42] = {'nonce': 'a' * 16, 'expires': time.monotonic() + 60}
        for view in ('home', 'chats', 'settings', 'status'):
            state = self.dashboard.render_state(view, page=10000, user_id=42)
            encoded = self.dashboard.encode_state(state)
            self.assertLessEqual(len(encoded.encode()), 1024)
            self.assertNotIn('secret@example.com', encoded)
            self.assertNotIn('private message', encoded)
            self.assertNotIn('private name', encoded)
            if view == 'chats':
                self.assertEqual(state['page'], 14)
                self.assertEqual(len(state['items']), 2)
                self.assertTrue(state['items'][0]['url'].startswith('https://t.me/c/1234567890/'))

    async def test_refresh_stops_for_expired_or_revoked_operator(self):
        self.register()
        self.dashboard._active[42] = time.monotonic() - 1000
        async def stop_after_sleep(_):
            self.service.running = False
        with patch('widget_dashboard.asyncio.sleep', side_effect=stop_after_sleep):
            await self.dashboard.refresh_loop()
        self.transport.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
