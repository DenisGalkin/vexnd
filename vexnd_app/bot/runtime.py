from __future__ import annotations

import os
import time

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from vexnd_app.bot.common import (
    BOT_API,
    BOT_TOKEN,
    HTTP,
    SUBSCRIPTION_REMINDER_CHECK_INTERVAL_SECONDS,
    app,
    db,
    seed_promo_codes,
)
from vexnd_app.bot.handlers.callbacks import handle_callback
from vexnd_app.bot.handlers.messages import handle_message
from vexnd_app.bot.subscriptions import dispatch_subscription_reminders


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
    offset = 0
    last_reminder_check = 0.0
    print("VEXND Telegram bot started")
    while True:
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
                offset = int(update["update_id"]) + 1
                try:
                    if "message" in update:
                        handle_message(update["message"])
                    elif "callback_query" in update:
                        handle_callback(update["callback_query"])
                except Exception as exc:
                    print(f"Update handling failed: {exc}")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"Polling failed: {exc}")
            time.sleep(5)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")
    with app.app_context():
        ensure_bot_schema()
        seed_promo_codes()
    poll_updates()


if __name__ == "__main__":
    main()
