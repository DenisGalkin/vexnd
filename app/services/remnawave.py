from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from requests import HTTPError

from app.core.config import HTTP, RemnawaveConfig
from app.domain.models import User

REMNAWAVE_TRAFFIC_LIMIT_BYTES = 2 * 1024 * 1024 * 1024 * 1024


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


def remnawave_raise_for_status(resp) -> None:
    try:
        resp.raise_for_status()
    except HTTPError as exc:
        details = ""
        try:
            payload = resp.json() or {}
            if isinstance(payload, dict):
                message = str(payload.get("message") or "").strip()
                errors = payload.get("errors")
                if isinstance(errors, list) and errors:
                    parts: list[str] = []
                    for item in errors:
                        if not isinstance(item, dict):
                            continue
                        path = ".".join(str(segment) for segment in (item.get("path") or []) if segment not in (None, ""))
                        text = str(item.get("message") or "").strip()
                        parts.append(f"{path}: {text}" if path and text else path or text)
                    if parts:
                        details = "; ".join(parts)
                if not details and message:
                    details = message
        except Exception:
            details = (resp.text or "").strip()
        if details:
            raise RuntimeError(f"Remnawave API error ({resp.status_code}): {details}") from exc
        raise


def remnawave_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def remnawave_response_user(data: object) -> Optional[dict]:
    if not isinstance(data, dict):
        return None
    response = data.get("response")
    if isinstance(response, dict):
        return response
    if isinstance(response, list):
        for item in response:
            if isinstance(item, dict):
                return item
        return None
    return None


def rw_username_from_email(email: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", (email or "").strip().lower())
    value = value.strip("-_")
    if len(value) < 3:
        value = f"user-{value}" if value else "user"
    return value[:36]


def rw_username_from_telegram(telegram_id: int | None, username: str | None = None) -> str:
    preferred = re.sub(r"[^a-zA-Z0-9_-]+", "-", (username or "").strip().lower()).strip("-_")
    if preferred:
        if len(preferred) < 3 and telegram_id:
            return f"tg-{int(telegram_id)}"[:36]
        return preferred[:36]
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
        from app.bot.models import TelegramAccount

        account = TelegramAccount.query.filter_by(user_id=user_id).first()
        if not account:
            return None, None
        return account.telegram_id, (account.username or None)
    except Exception:
        return None, None


def remnawave_uses_telegram_identity(user_or_email: User | str | None) -> bool:
    if isinstance(user_or_email, str) or not user_or_email:
        return False
    telegram_id, _username = telegram_account_identity(user_or_email.id)
    return bool(telegram_id)


def remnawave_primary_email(user_or_email: User | str | None) -> str | None:
    if isinstance(user_or_email, str):
        email = (user_or_email or "").strip().lower()
        return email or None
    if not user_or_email:
        return None
    email = (user_or_email.email or "").strip().lower()
    if not email or is_telegram_placeholder_email(email):
        return None
    return email


def remnawave_identity_candidates(
    user_or_email: User | str | None,
    *,
    include_email: bool = True,
    include_legacy_placeholder: bool = False,
) -> tuple[list[int], list[str], list[str], list[str]]:
    telegram_ids: list[int] = []
    emails: list[str] = []
    legacy_emails: list[str] = []
    usernames: list[str] = []
    if isinstance(user_or_email, str):
        email = (user_or_email or "").strip().lower()
        if email:
            emails.append(email)
            usernames.append(rw_username_from_email(email))
        return telegram_ids, emails, legacy_emails, usernames
    user = user_or_email
    if not user:
        return telegram_ids, emails, legacy_emails, usernames
    if include_email:
        email = remnawave_primary_email(user)
        if email:
            emails.append(email)
            usernames.append(rw_username_from_email(email))
    tg_id, tg_username = telegram_account_identity(user.id)
    if tg_id:
        telegram_ids.append(int(tg_id))
        usernames.append(rw_username_from_telegram(tg_id, tg_username))
        if include_legacy_placeholder:
            placeholder = telegram_local_placeholder_email(tg_id).lower()
            legacy_emails.append(placeholder)
    seen: set[str] = set()
    unique_usernames: list[str] = []
    for candidate in usernames:
        normalized = candidate.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_usernames.append(normalized)
    return (
        telegram_ids,
        list(dict.fromkeys(emails)),
        list(dict.fromkeys(legacy_emails)),
        unique_usernames,
    )


def remnawave_get_user_by_telegram_id(cfg: RemnawaveConfig, telegram_id: int) -> Optional[dict]:
    resp = HTTP.get(f"{cfg.base_url}/api/users/by-telegram-id/{int(telegram_id)}", headers=remnawave_headers(cfg), timeout=20)
    if resp.status_code == 404:
        return None
    remnawave_raise_for_status(resp)
    return remnawave_response_user(resp.json() or {})


def remnawave_get_user_by_email(cfg: RemnawaveConfig, email: str) -> Optional[dict]:
    resp = HTTP.get(f"{cfg.base_url}/api/users/by-email/{email}", headers=remnawave_headers(cfg), timeout=20)
    if resp.status_code == 404:
        return None
    remnawave_raise_for_status(resp)
    return remnawave_response_user(resp.json() or {})


def remnawave_get_user_by_username(cfg: RemnawaveConfig, username: str) -> Optional[dict]:
    resp = HTTP.get(f"{cfg.base_url}/api/users/by-username/{username}", headers=remnawave_headers(cfg), timeout=20)
    if resp.status_code == 404:
        return None
    remnawave_raise_for_status(resp)
    return remnawave_response_user(resp.json() or {})


def remnawave_get_user_by_uuid(cfg: RemnawaveConfig, user_uuid: str) -> Optional[dict]:
    resp = HTTP.get(f"{cfg.base_url}/api/users/{user_uuid}", headers=remnawave_headers(cfg), timeout=20)
    if resp.status_code == 404:
        return None
    remnawave_raise_for_status(resp)
    return remnawave_response_user(resp.json() or {})


def remnawave_find_user(cfg: RemnawaveConfig, user_or_email: User | str | None, *, include_email: bool = True):
    telegram_ids, emails, legacy_emails, usernames = remnawave_identity_candidates(
        user_or_email,
        include_email=include_email,
        include_legacy_placeholder=True,
    )
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
    for email in legacy_emails:
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


def remnawave_create_user(cfg: RemnawaveConfig, user_or_email: User | str | None, expiry_date: datetime, *, include_email: bool = True) -> dict:
    telegram_ids, emails, _legacy_emails, usernames = remnawave_identity_candidates(
        user_or_email,
        include_email=include_email,
    )
    payload = {
        "email": emails[0] if emails else None,
        "username": usernames[0] if usernames else None,
        "expireAt": remnawave_datetime(expiry_date),
        "status": "ACTIVE",
        "trafficLimitBytes": REMNAWAVE_TRAFFIC_LIMIT_BYTES,
        "trafficLimitStrategy": "MONTH",
        "activeInternalSquads": list(cfg.internal_squads),
    }
    if telegram_ids:
        payload["telegramId"] = int(telegram_ids[0])
    payload = {key: value for key, value in payload.items() if value not in (None, "", [])}
    resp = HTTP.post(f"{cfg.base_url}/api/users", headers=remnawave_headers(cfg), json=payload, timeout=30)
    remnawave_raise_for_status(resp)
    data = resp.json() or {}
    result = data.get("response") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("Invalid Remnawave create-user response")
    return result


def remnawave_sync_user_identity(
    cfg: RemnawaveConfig,
    user: User,
    *,
    remote_user: dict | None = None,
) -> dict | None:
    remote_user = remote_user or remnawave_find_user(cfg, user)
    if not isinstance(remote_user, dict):
        return None
    remote_uuid = str(remote_user.get("uuid") or "").strip()
    if not remote_uuid:
        return remote_user

    email = remnawave_primary_email(user)
    telegram_id, telegram_username = telegram_account_identity(user.id)
    desired_username = rw_username_from_email(email) if email else rw_username_from_telegram(telegram_id, telegram_username)

    current_email = str(remote_user.get("email") or "").strip().lower()
    current_username = str(remote_user.get("username") or "").strip()
    current_telegram_id = remote_user.get("telegramId")
    if current_telegram_id is None:
        current_telegram_id = remote_user.get("telegram_id")
    try:
        current_telegram_id = int(current_telegram_id) if current_telegram_id not in (None, "") else None
    except Exception:
        current_telegram_id = None

    payload: dict[str, object] = {"uuid": remote_uuid}
    changed = False
    if email and current_email != email:
        payload["email"] = email
        changed = True
    elif not email and is_telegram_placeholder_email(current_email):
        payload["email"] = None
        changed = True
    if telegram_id and current_telegram_id != int(telegram_id):
        payload["telegramId"] = int(telegram_id)
        changed = True
    elif not telegram_id and current_telegram_id is not None:
        payload["telegramId"] = None
        changed = True
    if desired_username and current_username != desired_username:
        payload["username"] = desired_username
        changed = True
    if not changed:
        return remote_user

    resp = HTTP.patch(f"{cfg.base_url}/api/users", headers=remnawave_headers(cfg), json=payload, timeout=30)
    remnawave_raise_for_status(resp)
    result = remnawave_response_user(resp.json() or {})
    return result or remnawave_get_user_by_uuid(cfg, remote_uuid) or remote_user


def remnawave_extend_user(cfg: RemnawaveConfig, user_uuid: str, extend_days: int) -> None:
    remote_user = remnawave_get_user_by_uuid(cfg, user_uuid) or {}
    current_expiry = parse_rw_datetime(remote_user.get("expireAt") or remote_user.get("expire_at"))
    base_dt = current_expiry if current_expiry and current_expiry > datetime.utcnow() else datetime.utcnow()
    new_expiry = base_dt + timedelta(days=max(int(extend_days), 1))
    resp = HTTP.patch(
        f"{cfg.base_url}/api/users",
        headers=remnawave_headers(cfg),
        json={"uuid": user_uuid, "expireAt": remnawave_datetime(new_expiry)},
        timeout=30,
    )
    remnawave_raise_for_status(resp)


def remnawave_update_user_traffic(
    cfg: RemnawaveConfig,
    user_uuid: str,
    limit_bytes: int = REMNAWAVE_TRAFFIC_LIMIT_BYTES,
    strategy: str = "MONTH",
) -> None:
    resp = HTTP.patch(
        f"{cfg.base_url}/api/users",
        headers=remnawave_headers(cfg),
        json={"uuid": user_uuid, "trafficLimitBytes": int(limit_bytes), "trafficLimitStrategy": strategy},
        timeout=30,
    )
    remnawave_raise_for_status(resp)


def remnawave_delete_user(cfg: RemnawaveConfig, user_uuid: str) -> bool:
    normalized_uuid = str(user_uuid or "").strip()
    if not normalized_uuid:
        return False

    resp = HTTP.delete(f"{cfg.base_url}/api/users/{normalized_uuid}", headers=remnawave_headers(cfg), timeout=30)
    if resp.status_code == 404:
        return False
    if resp.ok:
        return True

    bulk_resp = HTTP.post(
        f"{cfg.base_url}/api/users/bulk/delete",
        headers=remnawave_headers(cfg),
        json={"uuids": [normalized_uuid]},
        timeout=30,
    )
    if bulk_resp.status_code == 404:
        return False
    remnawave_raise_for_status(bulk_resp)
    return True


def delete_remnawave_user_for_local_user(user: User | None) -> bool:
    if not user:
        return False

    cfg = get_remnawave_config()
    if not (cfg.base_url and cfg.token):
        return False

    remote_user = remnawave_find_user(cfg, user)
    remote_uuid = str((remote_user or {}).get("uuid") or "").strip()
    if not remote_uuid:
        return False
    return remnawave_delete_user(cfg, remote_uuid)


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
    "remnawave_uses_telegram_identity",
    "parse_rw_datetime",
    "remnawave_create_user",
    "remnawave_delete_user",
    "remnawave_extend_user",
    "remnawave_find_user",
    "remnawave_get_user_by_uuid",
    "remnawave_headers",
    "remnawave_subscription_url_from_user",
    "remnawave_sync_user_identity",
    "remnawave_update_user_traffic",
    "rw_username_from_email",
    "rw_username_from_telegram",
    "delete_remnawave_user_for_local_user",
    "telegram_account_identity",
    "telegram_local_placeholder_email",
]
