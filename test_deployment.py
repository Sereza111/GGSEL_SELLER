import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from container_entrypoint import prepare_state
import container_entrypoint

UPSTREAM = Path(__file__).resolve().parent
sys.path.insert(0, str(UPSTREAM))
from autoresponder import AutoResponder


class DeploymentStateTests(unittest.TestCase):
    def test_native_entrypoint_uses_same_seeded_path_as_application(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_script = Path(directory) / 'container_entrypoint.py'
            auto_path = Path(directory) / 'state' / 'auto.json'
            db_path = Path(directory) / 'database' / 'state.db'
            (Path(directory) / '.env').write_text(
                f'AUTORESPONDER_CONFIG_PATH={auto_path.as_posix()}\nDATABASE_PATH={db_path.as_posix()}\n',
                encoding='utf-8',
            )
            with patch.dict(os.environ, {}, clear=True), patch.object(container_entrypoint, '__file__', str(fake_script)), \
                 patch.object(sys, 'argv', ['entrypoint', 'python', 'main.py']), patch.object(os, 'execvp') as execute:
                container_entrypoint.main()
                self.assertEqual(os.environ['AUTORESPONDER_CONFIG_PATH'], auto_path.as_posix())
                self.assertTrue(db_path.parent.is_dir())
                self.assertFalse(AutoResponder().is_enabled())
                execute.assert_called_once_with('python', ['python', 'main.py'])

    def test_first_boot_disables_every_automatic_customer_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "autoresponder.json"
            prepare_state(str(path))
            responder = AutoResponder(str(path))
            self.assertFalse(responder.is_enabled())
            self.assertFalse(responder.should_send_first_message())
            self.assertFalse(responder.is_review_responses_enabled())
            self.assertFalse(responder.config["csv_mode"]["enabled"])
            self.assertIsNone(responder.find_response("Здравствуйте"))

    def test_restart_preserves_operator_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "autoresponder.json"
            prepare_state(str(path))
            responder = AutoResponder(str(path))
            responder.config["enabled"] = True
            responder.config["first_message_enabled"] = True
            responder.config["first_message_text"] = "Operator-defined greeting"
            responder.save_config()
            original = path.read_bytes()
            prepare_state(str(path))
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_state_stops_before_bot_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "autoresponder.json"
            for content in ("not-json", "[]"):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    prepare_state(str(path))

    def test_environment_selects_persistent_autoresponder_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "autoresponder.json"
            prepare_state(str(path))
            with patch.dict(os.environ, {"AUTORESPONDER_CONFIG_PATH": str(path)}):
                responder = AutoResponder()
                responder.set_notify_text("Saved in persistent state")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["notify_text"], "Saved in persistent state")
            self.assertFalse(saved["enabled"])

    def test_log_survives_preliminary_logging_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONPATH=str(UPSTREAM))
            result = subprocess.run(
                [sys.executable, "-c", "import logging, main; logging.info('early'); main.setup_logging(); logging.info('persistent-log-check'); logging.shutdown()"],
                cwd=directory, env=env, capture_output=True, text=True, check=True,
            )
            self.assertIn("persistent-log-check", result.stdout)
            self.assertIn("persistent-log-check", (Path(directory) / "ggsel_bot.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
