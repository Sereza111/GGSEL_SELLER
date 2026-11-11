"""Explicitly enabled, trusted Python extensions for buyer text commands.

Discovery reads JSON only. Execution is isolated for reliability, not security:
installed Python still has the OS permissions of the service account.
"""

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys


_RUNNER = r'''
import asyncio, contextlib, importlib.util, inspect, json, os, sys
payload = json.load(sys.stdin)
try:
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        spec = importlib.util.spec_from_file_location('ggsel_extension', payload['path'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reply = module.on_message(payload['message'])
        if inspect.isawaitable(reply):
            reply = asyncio.run(reply)
    if reply is not None and (not isinstance(reply, str) or len(reply) > 4000):
        raise ValueError('Invalid reply')
    result = {'ok': True, 'response': reply}
except BaseException:
    result = {'ok': False}
sys.stdout.write(json.dumps(result, ensure_ascii=True))
'''


class PluginManager:
    MAX_PLUGINS = 16
    MAX_ENABLED = 4
    MAX_MANIFEST_BYTES = 16 * 1024
    MAX_MODULE_BYTES = 256 * 1024
    CALL_TIMEOUT = 2.0
    TOTAL_TIMEOUT = 5.0
    ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

    def __init__(self, db, directory=None):
        self.db = db
        self.directory = Path(directory or os.getenv("PLUGINS_DIR", "/data/plugins")).resolve()
        # One child at a time across all chats; parallel polls cannot spawn an
        # unbounded process pool. Every invocation also has a total time budget.
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(plugin_id):
        return "plugin_enabled:" + plugin_id

    def _inspect(self, manifest):
        plugin_id = manifest.stem
        result = {
            "id": plugin_id, "name": plugin_id, "version": "",
            "enabled": False, "error": "", "_fingerprint": None, "_path": None,
        }
        try:
            if not self.ID_PATTERN.fullmatch(plugin_id):
                raise ValueError("Недопустимое имя файла манифеста")
            if manifest.is_symlink() or manifest.resolve().parent != self.directory:
                raise ValueError("Ссылки на файлы вне папки плагинов запрещены")
            if manifest.stat().st_size > self.MAX_MANIFEST_BYTES:
                raise ValueError("Манифест слишком большой")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("id") != plugin_id:
                raise ValueError("ID должен совпадать с именем JSON-файла")
            name = data.get("name")
            version = data.get("version")
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise ValueError("В манифесте нужно имя длиной до 80 символов")
            if not isinstance(version, str) or not version.strip() or len(version) > 32:
                raise ValueError("В манифесте нужна версия длиной до 32 символов")
            module = data.get("module")
            if not isinstance(module, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}\.py", module):
                raise ValueError("module должен содержать только имя Python-файла")
            module_path = self.directory / module
            if module_path.is_symlink() or module_path.resolve().parent != self.directory:
                raise ValueError("Ссылки на файлы вне папки плагинов запрещены")
            if not module_path.is_file() or module_path.stat().st_size > self.MAX_MODULE_BYTES:
                raise ValueError("Python-файл отсутствует или превышает 256 КиБ")
            source = module_path.read_bytes()
            fingerprint = hashlib.sha256(
                json.dumps(data, sort_keys=True, ensure_ascii=True).encode() + b"\0" + source
            ).hexdigest()
            saved = self.db.get_setting(self._key(plugin_id))
            result.update(name=name.strip(), version=version.strip(), _path=str(module_path),
                          _fingerprint=fingerprint, enabled=bool(saved))
            if saved and saved != fingerprint:
                result["error"] = "Файлы изменились: выключите и снова включите плагин"
            elif saved:
                result["error"] = self.db.get_setting("plugin_error:" + plugin_id) or ""
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            # Only our validation messages can be exposed, never file contents
            # or JSON parser errors which may quote them.
            result["error"] = (str(exc) if type(exc) is ValueError
                               else "Не удалось прочитать корректный манифест или Python-файл")
        return result

    def _discover(self):
        if not self.directory.is_dir():
            return []
        manifests = sorted(self.directory.glob("*.json"))[:self.MAX_PLUGINS]
        return [self._inspect(path) for path in manifests]

    def list_plugins(self):
        return [{key: value for key, value in item.items() if not key.startswith("_")}
                for item in self._discover()]

    def set_enabled(self, plugin_id, enabled):
        if not isinstance(plugin_id, str) or not self.ID_PATTERN.fullmatch(plugin_id):
            raise ValueError("Некорректный ID плагина")
        if not isinstance(enabled, bool):
            raise ValueError("Ожидается состояние включено/выключено")
        if not enabled:
            self.db.set_setting(self._key(plugin_id), "")
            self.db.set_setting("plugin_error:" + plugin_id, "")
            return
        plugins = self._discover()
        item = next((p for p in plugins if p["id"] == plugin_id), None)
        if not item or not item["_fingerprint"]:
            raise ValueError("Плагин не найден или его файлы некорректны")
        if sum(p["enabled"] for p in plugins if p["id"] != plugin_id) >= self.MAX_ENABLED:
            raise ValueError("Одновременно можно включить не более 4 плагинов")
        self.db.set_setting(self._key(plugin_id), item["_fingerprint"])
        self.db.set_setting("plugin_error:" + plugin_id, "")

    @staticmethod
    def _child_environment():
        # Do not inherit API keys, Telegram token, proxy credentials or .env.
        allowed = ("SYSTEMROOT", "WINDIR", "COMSPEC", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL")
        return {key: os.environ[key] for key in allowed if key in os.environ}

    async def _invoke(self, item, message, timeout):
        options = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", _RUNNER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=self._child_environment(),
            **options,
        )
        try:
            payload = json.dumps({"path": item["_path"], "message": message}, ensure_ascii=True).encode()
            output, _ = await asyncio.wait_for(process.communicate(payload), timeout)
            if process.returncode != 0 or len(output) > 32 * 1024:
                raise ValueError("Некорректный результат плагина")
            result = json.loads(output)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("Ошибка выполнения плагина")
            response = result.get("response")
            if response is not None and (not isinstance(response, str) or len(response) > 4000):
                raise ValueError("Некорректный ответ плагина")
            if response:
                response = "".join(ch for ch in response if ch in "\n\r\t" or ord(ch) >= 32)
                if response.strip():
                    return {"response": response, "notify_group": False, "notify_text": ""}
            return None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def find_response(self, message):
        if not isinstance(message, str) or not message.strip():
            return None
        # Serialize plugin jobs so concurrent customer polls cannot create an
        # unbounded process pool. Time spent evaluating each message is bounded.
        async with self._lock:
            deadline = asyncio.get_running_loop().time() + self.TOTAL_TIMEOUT
            for item in [p for p in self._discover() if p["enabled"] and not p["error"]][:self.MAX_ENABLED]:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    response = await self._invoke(item, message[:4000], min(self.CALL_TIMEOUT, remaining))
                    if response:
                        return response
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = ("Превышено время ответа" if isinstance(exc, asyncio.TimeoutError)
                             else "Ошибка выполнения плагина")
                    self.db.set_setting("plugin_error:" + item["id"],
                                        error + "; включите заново после исправления")
                    logging.warning("Plugin %s suspended: %s", item["id"], error)
        return None
