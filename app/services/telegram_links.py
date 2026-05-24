from __future__ import annotations

import os

import requests


_BOT_USERNAME_CACHE: str | None = None


def _resolve_bot_username() -> str | None:
    global _BOT_USERNAME_CACHE
    if _BOT_USERNAME_CACHE:
        return _BOT_USERNAME_CACHE

    username = (os.environ.get("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
    if username:
        _BOT_USERNAME_CACHE = username
        return username

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return None

    try:
        resp = requests.post(f"https://api.telegram.org/bot{token}/getMe", json={}, timeout=10)
        data = resp.json()
        username = str(((data.get("result") or {}).get("username") or "")).strip().lstrip("@")
        if username:
            _BOT_USERNAME_CACHE = username
            return username
    except Exception:
        return None
    return None


def telegram_bot_deeplink(start_value: str) -> str | None:
    username = _resolve_bot_username()
    if not username:
        return None
    return f"https://t.me/{username}?start={start_value}"
