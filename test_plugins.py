import asyncio
import gc
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from database import Database
from plugin_manager import PluginManager


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # SQLite's context manager commits but the legacy Database helpers do
        # not close their connections. Collect them before Windows removes DB.
        self.addCleanup(gc.collect)
        self.root = Path(self.temp.name)
        self.plugin_dir = self.root / "plugins"
        self.plugin_dir.mkdir()
        self.database = Database(str(self.root / "test.db"))
        self.manager = PluginManager(self.database, self.plugin_dir)

    def install(self, plugin_id="help_command", source=None, **manifest_changes):
        source = source or "def on_message(message):\n    return 'Help' if message == '!help' else None\n"
        module_path = self.plugin_dir / f"{plugin_id}.py"
        module_path.write_text(source, encoding="utf-8")
        manifest = {"id": plugin_id, "name": "Help command", "version": "1.0",
                    "module": module_path.name}
        manifest.update(manifest_changes)
        (self.plugin_dir / f"{plugin_id}.json").write_text(json.dumps(manifest), encoding="utf-8")
        return module_path

    async def test_discovery_and_disabled_matching_never_execute_plugin(self):
        marker = self.root / "executed"
        self.install(source=f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef on_message(message):\n    return 'reply'\n")
        rows = self.manager.list_plugins()
        self.assertEqual(rows[0]["id"], "help_command")
        self.assertFalse(rows[0]["enabled"])
        self.assertIsNone(await self.manager.find_response("!help"))
        self.assertFalse(marker.exists())

    async def test_enabled_plugin_persists_and_returns_only_matching_reply(self):
        self.install()
        self.manager.set_enabled("help_command", True)
        restored = PluginManager(self.database, self.plugin_dir)
        self.assertTrue(restored.list_plugins()[0]["enabled"])
        self.assertIsNone(await restored.find_response("hello"))
        self.assertEqual(await restored.find_response("!help"),
                         {"response": "Help", "notify_group": False, "notify_text": ""})
        restored.set_enabled("help_command", False)
        self.assertIsNone(await restored.find_response("!help"))

    async def test_child_does_not_receive_credentials(self):
        self.install(source="import os\ndef on_message(message):\n    return 'clean' if not any(k in os.environ for k in ('TELEGRAM_BOT_TOKEN', 'GGSEL_API_KEY', 'OPENAI_API_KEY')) else 'leaked'\n")
        self.manager.set_enabled("help_command", True)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "do-not-inherit",
                                    "GGSEL_API_KEY": "do-not-inherit",
                                    "OPENAI_API_KEY": "do-not-inherit"}):
            reply = await self.manager.find_response("hello")
        self.assertEqual(reply["response"], "clean")

    async def test_source_changes_require_explicit_reenable(self):
        module = self.install()
        self.manager.set_enabled("help_command", True)
        module.write_text("def on_message(message):\n    return 'changed'\n", encoding="utf-8")
        self.assertIn("изменились", self.manager.list_plugins()[0]["error"])
        self.assertIsNone(await self.manager.find_response("!help"))
        self.manager.set_enabled("help_command", True)
        self.assertEqual((await self.manager.find_response("!help"))["response"], "changed")

    async def test_traversal_manifest_cannot_be_enabled(self):
        self.install(module="../external.py")
        self.assertTrue(self.manager.list_plugins()[0]["error"])
        with self.assertRaises(ValueError):
            self.manager.set_enabled("help_command", True)

    async def test_exception_does_not_leak_text_and_suspend_survives_restart(self):
        self.install(source="def on_message(message):\n    raise RuntimeError(message)\n")
        self.manager.set_enabled("help_command", True)
        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(await self.manager.find_response("private-buyer-text"))
        self.assertNotIn("private-buyer-text", "\n".join(logs.output))
        restored = PluginManager(self.database, self.plugin_dir)
        self.assertIn("Ошибка выполнения", restored.list_plugins()[0]["error"])
        self.assertIsNone(await restored.find_response("hello"))

    async def test_timeout_terminates_child_and_other_plugin_still_works(self):
        self.install("a_slow", "import time\ndef on_message(message):\n    time.sleep(60)\n")
        self.install("b_help")
        self.manager.set_enabled("a_slow", True)
        self.manager.set_enabled("b_help", True)
        result = await asyncio.wait_for(self.manager.find_response("!help"), 7)
        self.assertEqual(result["response"], "Help")
        self.assertIn("Превышено", self.manager.list_plugins()[0]["error"])

    async def test_async_handler_and_prints_do_not_corrupt_protocol(self):
        self.install(source="print('import noise')\nasync def on_message(message):\n    print('callback noise')\n    return 'async reply'\n")
        self.manager.set_enabled("help_command", True)
        self.assertEqual((await self.manager.find_response("hi"))["response"], "async reply")

    async def test_rejects_oversized_reply_and_limits_enabled_plugins(self):
        self.install("a_big", "def on_message(message):\n    return 'x' * 4001\n")
        for plugin_id in ("b_help", "c_help", "d_help", "e_help"):
            self.install(plugin_id)
        for plugin_id in ("a_big", "b_help", "c_help", "d_help"):
            self.manager.set_enabled(plugin_id, True)
        with self.assertRaisesRegex(ValueError, "не более 4"):
            self.manager.set_enabled("e_help", True)
        result = await self.manager.find_response("!help")
        self.assertEqual(result["response"], "Help")
        self.assertTrue(self.manager.list_plugins()[0]["error"])

    async def test_only_one_child_runs_during_parallel_chat_checks(self):
        self.install()
        self.manager.set_enabled("help_command", True)
        active = 0
        max_active = 0

        async def invoke(*args):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return None

        with patch.object(self.manager, "_invoke", side_effect=invoke):
            await asyncio.gather(*(self.manager.find_response("hello") for _ in range(8)))
        self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()
