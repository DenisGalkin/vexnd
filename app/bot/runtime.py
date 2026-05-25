from __future__ import annotations

import os
import signal
import time

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from app.bot.common import (
    BOT_API,
    BOT_TOKEN,
    HTTP,
    SUBSCRIPTION_REMINDER_CHECK_INTERVAL_SECONDS,
    app,
    db,
)
from app.bot.handlers.callbacks import handle_callback
from app.bot.handlers.messages import handle_message
from app.bot.subscriptions import dispatch_subscription_reminders

_stop_requested = False


class StopPolling(SystemExit):
    pass


def _request_stop(signum: int, _frame) -> None:
    global _stop_requested
    _stop_requested = True
    print(f"Stop signal received: {signum}")
    raise StopPolling(0)


def ensure_bot_schema() -> None:
    lock_path = os.environ.get("DB_SCHEMA_LOCK_FILE", "/tmp/vexnd_db_schema.lock")
    if fcntl is None:
        db.create_all()
        return
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            db.create_all()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def poll_updates() -> None:
    global _stop_requested
    offset = 0
    last_reminder_check = 0.0
    print("VEXND Telegram bot started")
    while not _stop_requested:
        try:
            now_ts = time.time()
            if now_ts - last_reminder_check >= SUBSCRIPTION_REMINDER_CHECK_INTERVAL_SECONDS:
                with app.app_context():
                    dispatch_subscription_reminders()
                last_reminder_check = now_ts
            resp = HTTP.get(
                f"{BOT_API}/getUpdates",
                params={"offset": offset, "timeout": 30, "allowed_updates": '["message","callback_query"]'},
                timeout=35,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data)
            for update in data.get("result", []):
                if _stop_requested:
                    break
                offset = int(update["update_id"]) + 1
                try:
                    if "message" in update:
                        handle_message(update["message"])
                    elif "callback_query" in update:
                        handle_callback(update["callback_query"])
                except Exception as exc:
                    print(f"Update handling failed: {exc}")
        except (KeyboardInterrupt, StopPolling):
            _stop_requested = True
        except Exception as exc:
            if _stop_requested:
                break
            print(f"Polling failed: {exc}")
            time.sleep(5)
    print("VEXND Telegram bot stopped")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    with app.app_context():
        ensure_bot_schema()
    poll_updates()


if __name__ == "__main__":
    main()
