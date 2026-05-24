from __future__ import annotations

import os


def telegram_bot_deeplink(start_value: str) -> str | None:
    username = (os.environ.get("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}?start={start_value}"
