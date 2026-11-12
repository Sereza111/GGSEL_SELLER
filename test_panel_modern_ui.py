import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import telegram

import operator_panel
from operator_panel import OperatorPanel


class ModernOperatorPanelTests(unittest.TestCase):
    def setUp(self):
        self.service = SimpleNamespace(
            sync_enabled=True,
            awaiting_input={},
            database=SimpleNamespace(get_setting=Mock(return_value="true")),
            telegram_bot=SimpleNamespace(public_username="GGsel_demo_bot", edit_message=AsyncMock()),
            topic_manager=SimpleNamespace(get_all_topics=Mock(return_value={
                "1": {"invoice_id": 1, "topic_id": 11},
                "2": {"invoice_id": 2, "topic_id": 12},
                "pending": {"invoice_id": 3},
            })),
            autoresponder=SimpleNamespace(get_triggers=Mock(return_value=[{}, {}, {}]),
                                         is_enabled=Mock(return_value=False)),
            config=SimpleNamespace(telegram_group_id=-1001234567890, telegram_allowed_user_ids={42}),
        )
        # Other test modules stub Telegram during import; assert against real buttons.
        buttons = patch.object(operator_panel, "InlineKeyboardButton", telegram.InlineKeyboardButton)
        buttons.start()
        self.addCleanup(buttons.stop)
        self.panel = OperatorPanel(self.service)

    def test_main_menu_keeps_all_operator_actions_and_topic_reply_instruction(self):
        text, keyboard = self.panel.main_menu()
        callbacks = {button.callback_data for row in keyboard for button in row if button.callback_data}
        self.assertEqual(callbacks, {
            "panel:chats:0", "panel:status", "auto_triggers", "auto_menu",
            "panel:notifications", "panel:plugins:0", "panel:config",
            "check_balance", "stats", "panel:help", "close",
        })
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in callbacks))
        self.assertIn("его тему через «Чаты и ответы» и напишите текст", text)

    def test_main_menu_shows_counts_for_linked_chats_and_configured_commands(self):
        text, _ = self.panel.main_menu()
        self.assertIn("Чатов: 2 · Команд: 3", text)
        self.assertIn("Синхронизация включена", text)
        self.assertIn("Уведомления о сообщениях включены", text)
        self.assertIn("Автоответы выключены", text)
        self.service.sync_enabled = False
        self.service.database.get_setting.return_value = "false"
        text, _ = self.panel.main_menu()
        self.assertIn("Синхронизация остановлена", text)
        self.assertIn("Уведомления о сообщениях выключены", text)

    def test_personal_dashboard_link_is_prominent_and_uses_verified_username(self):
        text, keyboard = self.panel.main_menu()
        self.assertEqual(len(keyboard[0]), 1)
        button = keyboard[0][0]
        self.assertEqual(button.url, "https://t.me/GGsel_demo_bot?start=dashboard")
        self.assertIsNone(button.callback_data)
        self.assertEqual(button.to_dict()["style"], "primary")
        self.assertIn("личном чате", text)

    def test_missing_or_invalid_username_omits_link_but_keeps_group_actions(self):
        for username in (None, "", "bot", "@GGsel_demo_bot", "bad?start=other",
                         "https://t.me/bot", "good_bot\n", "a" * 33, "<b>evil</b>", Mock()):
            with self.subTest(username=username):
                self.service.telegram_bot.public_username = username
                text, keyboard = self.panel.main_menu()
                self.assertFalse(any(button.url for row in keyboard for button in row))
                self.assertEqual(keyboard[0][0].callback_data, "panel:chats:0")
                self.assertNotIn("<b>", text)
        del self.service.telegram_bot.public_username
        self.assertFalse(any(button.url for row in self.panel.main_menu()[1] for button in row))

    def test_button_styles_preserve_callback_and_label_for_older_clients(self):
        for action, expected in (("panel:chats:0", "primary"), ("panel:pause", "danger"),
                                 ("panel:resume", "success"), ("panel:config_apply:abc", "primary")):
            with self.subTest(action=action):
                button = self.panel.button("Открыть", action)
                data = button.to_dict()
                self.assertEqual(data["style"], expected)
                self.assertEqual(data["callback_data"], action)
                self.assertEqual(data["text"], "Открыть")
        self.assertNotIn("style", self.panel.button("Назад", "panel:main").to_dict())
        self.assertNotIn("style", self.panel.button("Закрыть", "close").to_dict())
        explicit = self.panel.button("Включить", "panel:notify_toggle", "success")
        self.assertEqual(explicit.to_dict()["style"], "success")
        with self.assertRaises(ValueError):
            self.panel.button("Неверно", "panel:main", "rainbow")

    def _update(self):
        chat = SimpleNamespace(id=self.service.config.telegram_group_id)
        message = SimpleNamespace(message_id=10, chat=chat, message_thread_id=1)
        return SimpleNamespace(effective_chat=chat, effective_user=SimpleNamespace(id=42),
                               effective_message=message, callback_query=SimpleNamespace(message=message))

    def test_notification_style_tracks_resulting_action(self):
        for enabled, style in ((True, "danger"), (False, "success")):
            self.service.database.get_setting.return_value = str(enabled).lower()
            asyncio.run(self.panel._notifications(self._update()))
            keyboard = self.service.telegram_bot.edit_message.await_args.args[3]
            self.assertEqual(keyboard[0][0].callback_data, "panel:notify_toggle")
            self.assertEqual(keyboard[0][0].to_dict()["style"], style)

    def test_help_explains_private_panel_group_settings_and_customer_replies(self):
        asyncio.run(self.panel.handle_callback("panel:help", self._update(), None))
        text = self.service.telegram_bot.edit_message.await_args.args[2]
        self.assertIn("личном чате с ботом", text)
        self.assertIn("В группе /menu", text)
        self.assertIn("обычный текст в его теме", text)
        self.assertIn("Команды управления не отправляются покупателю", text)


if __name__ == "__main__":
    unittest.main()
