from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

from vexnd_app.config import HTTP, RemnawaveConfig
from vexnd_app.models import Subscription, User


def get_remnawave_config() -> RemnawaveConfig:
    base = os.environ.get("REMNAWAVE_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("REMNAWAVE_TOKEN", "").strip()
    x_api_key = os.environ.get("REMNAWAVE_X_API_KEY", "").strip() or None
    squads_raw = os.environ.get("REMNAWAVE_INTERNAL_SQUADS", "").strip()
    squads = tuple(s.strip() for s in squads_raw.split(",") if s.strip())
    return RemnawaveConfig(base_url=base, token=token, x_api_key=x_api_key, internal_squads=squads)


def remnawave_headers(cfg: RemnawaveConfig) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {cfg.token}", "Accept": "application/json", "Content-Type": "application/json"}
    if cfg.x_api_key:
        headers["X-API-Key"] = cfg.x_api_key
    return headers


def rw_username_from_email(email: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", (email or "").strip().lower())
    value = value.strip(".-_")
    return value[:48] or "user"


def rw_username_from_telegram(telegram_id: int | None, username: str | None = None) -> str:
    preferred = re.sub(r"[^a-zA-Z0-9._-]+", "-", (username or "").strip().lower()).strip(".-_")
    if preferred:
        return preferred[:48]
    if telegram_id:
        return f"tg-{int(telegram_id)}"
    return "telegram-user"


def is_telegram_placeholder_email(email: str | None) -> bool:
    value = (email or "").strip().lower()
    return bool(value) and value.endswith("@telegram.local")


def telegram_local_placeholder_email(telegram_id: int | None) -> str:
    return f"tg-{int(telegram_id or 0)}@telegram.local"


def telegram_account_identity(user_id: int | None) -> tuple[int | None, str | None]:
    if not user_id:
        return None, None
    try:
        from vexnd_bot.models import TelegramAccount

        account = TelegramAccount.query.filter_by(user_id=user_id).first()
        if not account:
            return None, None
        return account.telegram_id, (account.username or None)
    except Exception:
        return None, None


def remnawave_identity_candidates(user_or_email: User | str | None) -> tuple[list[int], list[str], list[str]]:
    telegram_ids: list[int] = []
    emails: list[str] = []
    usernames: list[str] = []
    if isinstance(user_or_email, str):
        email = (user_or_email or "").strip().lower()
        if email:
            emails.append(email)
            usernames.append(rw_username_from_email(email))
        return telegram_ids, emails, usernames
    user = user_or_email
    if not user:
        return telegram_ids, emails, usernames
    tg_id, tg_username = telegram_account_identity(user.id)
    if tg_id:
        telegram_ids.append(int(tg_id))
        usernames.append(rw_username_from_telegram(tg_id, tg_username))
        placeholder = telegram_local_placeholder_email(tg_id).lower()
        if placeholder not in emails:
            emails.append(placeholder)
    email = (user.email or "").strip().lower()
    if email:
        emails.append(email)
        if not is_telegram_placeholder_email(email):
            usernames.append(rw_username_from_email(email))
    seen: set[str] = set()
    unique_usernames: list[str] = []
    for candidate in usernames:
        normalized = candidate.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_usernames.append(normalized)
    return telegram_ids, list(dict.fromkeys(emails)), unique_usernames


def remnawave_get_user_by_telegram_id(cfg: RemnawaveConfig, telegram_id: int) -> Optional[dict]:
    resp = HTTP.get(f"{cfg.base_url}/api/users/by-telegram/{int(telegram_id)}", headers=remnawave_headers(cfg), timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json() or {}
    return data.get("response") if isinstance(data, dict) else None


def remnawave_get_user_by_email(cfg: RemnawaveConfig, email: str) -> Optional[dict]:
    resp = HTTP.get(f"{cfg.base_url}/api/users/by-email/{email}", headers=remnawave_headers(cfg), timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json() or {}
    return data.get("response") if isinstance(data, dict) else None


def remnawave_get_user_by_username(cfg: RemnawaveConfig, username: str) -> Optional[dict]:
    resp = HTTP.get(f"{cfg.base_url}/api/users/by-username/{username}", headers=remnawave_headers(cfg), timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json() or {}
    return data.get("response") if isinstance(data, dict) else None


def remnawave_find_user(cfg: RemnawaveConfig, user_or_email: User | str | None):
    telegram_ids, emails, usernames = remnawave_identity_candidates(user_or_email)
    for telegram_id in telegram_ids:
        try:
            user = remnawave_get_user_by_telegram_id(cfg, telegram_id)
            if user:
                return user
        except Exception:
            pass
    for email in emails:
        try:
            user = remnawave_get_user_by_email(cfg, email)
            if user:
                return user
        except Exception:
            pass
    for username in usernames:
        try:
            user = remnawave_get_user_by_username(cfg, username)
            if user:
                return user
        except Exception:
            pass
    return None


def remnawave_create_user(cfg: RemnawaveConfig, user_or_email: User | str | None, expiry_date: datetime) -> dict:
    telegram_ids, emails, usernames = remnawave_identity_candidates(user_or_email)
    payload = {
        "email": emails[0] if emails else None,
        "username": usernames[0] if usernames else None,
        "expireAt": int(expiry_date.timestamp() * 1000),
        "internalSquads": list(cfg.internal_squads),
    }
    if telegram_ids:
        payload["telegramId"] = int(telegram_ids[0])
    payload = {key: value for key, value in payload.items() if value not in (None, "", [])}
    resp = HTTP.post(f"{cfg.base_url}/api/users", headers=remnawave_headers(cfg), json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json() or {}
    result = data.get("response") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("Invalid Remnawave create-user response")
    return result


def remnawave_extend_user(cfg: RemnawaveConfig, user_uuid: str, extend_days: int) -> None:
    resp = HTTP.post(
        f"{cfg.base_url}/api/users/{user_uuid}/extend",
        headers=remnawave_headers(cfg),
        json={"extendDays": int(extend_days)},
        timeout=30,
    )
    resp.raise_for_status()


def remnawave_update_user_traffic(cfg: RemnawaveConfig, user_uuid: str, limit_bytes: int = 1099511627776, strategy: str = "MONTH") -> None:
    resp = HTTP.post(
        f"{cfg.base_url}/api/users/{user_uuid}/traffic",
        headers=remnawave_headers(cfg),
        json={"trafficLimitBytes": int(limit_bytes), "trafficLimitStrategy": strategy},
        timeout=30,
    )
    resp.raise_for_status()


def remnawave_subscription_url_from_user(remote_user: dict | None) -> str:
    if not isinstance(remote_user, dict):
        return ""
    return str(remote_user.get("subscriptionUrl") or "").strip()


def parse_rw_datetime(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(str(dt_str).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


__all__ = [
    "get_remnawave_config",
    "is_telegram_placeholder_email",
    "parse_rw_datetime",
    "remnawave_create_user",
    "remnawave_extend_user",
    "remnawave_find_user",
    "remnawave_headers",
    "remnawave_subscription_url_from_user",
    "remnawave_update_user_traffic",
    "rw_username_from_email",
    "rw_username_from_telegram",
    "telegram_account_identity",
    "telegram_local_placeholder_email",
]
