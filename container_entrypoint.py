"""Initialize persistent state before any bot or network code is imported."""

import json
import os
from pathlib import Path
import sys


INITIAL_AUTORESPONDER_CONFIG = {
    "enabled": False,
    "first_message_enabled": False,
    "triggers": [],
    "review_responses": {
        "enabled": False,
        "good_enabled": False,
        "bad_enabled": False,
    },
    "csv_mode": {"enabled": False, "rules": []},
}


def prepare_state(config_path: str) -> None:
    """Seed disabled automation once; never overwrite an operator's settings."""
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Malformed state must not reach the upstream enabled default fallback.
        with path.open(encoding="utf-8") as stream:
            if not isinstance(json.load(stream), dict):
                raise ValueError("Auto-reply configuration must be a JSON object")
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(INITIAL_AUTORESPONDER_CONFIG, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", interpolate=False)
    os.environ.setdefault("AUTORESPONDER_CONFIG_PATH", "data/autoresponder.json")
    os.environ.setdefault("DATABASE_PATH", "data/ggsel_bot.db")
    os.environ.setdefault("PLUGINS_DIR", "data/plugins")
    try:
        prepare_state(os.environ["AUTORESPONDER_CONFIG_PATH"])
        Path(os.environ["DATABASE_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, TypeError):
        raise SystemExit("Cannot initialize auto-reply state; bot was not started.")
    if len(sys.argv) < 2:
        raise SystemExit("Missing bot command")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
