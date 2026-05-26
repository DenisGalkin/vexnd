from __future__ import annotations

import secrets
from datetime import datetime

from flask import request, session

from app.core.extensions import db
from app.domain.models import WebSession
from app.services.geoip import lookup_ip_location
from app.services.security import client_ip


WEB_SESSION_TOKEN_KEY = "web_session_token"


def current_web_session_token() -> str | None:
    token = (session.get(WEB_SESSION_TOKEN_KEY) or "").strip()
    return token or None


def clear_current_web_session_token() -> None:
    session.pop(WEB_SESSION_TOKEN_KEY, None)


def ensure_current_web_session(user_id: int) -> tuple[str | None, bool]:
    token = current_web_session_token()
    now = datetime.utcnow()
    ip = client_ip()
    user_agent = (request.headers.get("User-Agent") or "")[:255] or None
    path = (request.path or "")[:255] or None

    if token:
        existing = WebSession.query.filter_by(session_token=token).first()
        if existing and existing.user_id == int(user_id):
            if existing.revoked_at is not None:
                return token, True
            existing.last_ip = ip
            existing.last_user_agent = user_agent
            existing.last_path = path
            existing.last_seen_at = now
            db.session.commit()
            return token, False

    token = secrets.token_urlsafe(32)
    session[WEB_SESSION_TOKEN_KEY] = token
    db.session.add(
        WebSession(
            session_token=token,
            user_id=int(user_id),
            last_ip=ip,
            last_user_agent=user_agent,
            last_path=path,
            created_at=now,
            last_seen_at=now,
        )
    )
    db.session.commit()
    return token, False


def revoke_current_web_session(user_id: int) -> None:
    token = current_web_session_token()
    if not token:
        clear_current_web_session_token()
        return
    current = WebSession.query.filter_by(session_token=token, user_id=int(user_id)).first()
    if current and current.revoked_at is None:
        current.revoked_at = datetime.utcnow()
        db.session.commit()
    clear_current_web_session_token()


def revoke_user_web_session(user_id: int, session_id: int, *, current_token: str | None = None) -> bool:
    target = WebSession.query.filter_by(id=int(session_id), user_id=int(user_id), revoked_at=None).first()
    if not target:
        return False
    if current_token and target.session_token == current_token:
        return False
    target.revoked_at = datetime.utcnow()
    db.session.commit()
    return True


def revoke_other_web_sessions(user_id: int, *, current_token: str | None = None) -> int:
    query = WebSession.query.filter(
        WebSession.user_id == int(user_id),
        WebSession.revoked_at.is_(None),
    )
    if current_token:
        query = query.filter(WebSession.session_token != current_token)
    updated = query.update({"revoked_at": datetime.utcnow()}, synchronize_session=False)
    db.session.commit()
    return int(updated or 0)


def _ua_contains(user_agent: str, *parts: str) -> bool:
    return any(part in user_agent for part in parts)


def parse_user_agent(user_agent: str | None) -> tuple[str, str]:
    raw = (user_agent or "").lower()

    if _ua_contains(raw, "edg/"):
        browser = "Microsoft Edge"
    elif _ua_contains(raw, "opr/", "opera"):
        browser = "Opera"
    elif _ua_contains(raw, "chrome/", "crios/") and "edg/" not in raw:
        browser = "Google Chrome"
    elif _ua_contains(raw, "firefox/", "fxios/"):
        browser = "Mozilla Firefox"
    elif _ua_contains(raw, "safari/") and "chrome/" not in raw and "crios/" not in raw:
        browser = "Safari"
    else:
        browser = "Browser"

    if _ua_contains(raw, "iphone", "ipad", "ios"):
        os_name = "iOS"
    elif "android" in raw:
        os_name = "Android"
    elif _ua_contains(raw, "mac os x", "macintosh"):
        os_name = "macOS"
    elif "windows" in raw:
        os_name = "Windows"
    elif "linux" in raw:
        os_name = "Linux"
    else:
        os_name = "Unknown OS"

    return browser, os_name


def user_web_sessions(user_id: int, *, current_token: str | None = None, locale: str = "en") -> list[dict[str, object]]:
    rows = (
        WebSession.query.filter(
            WebSession.user_id == int(user_id),
            WebSession.revoked_at.is_(None),
        )
        .order_by(WebSession.last_seen_at.desc(), WebSession.created_at.desc())
        .all()
    )
    result: list[dict[str, object]] = []
    for row in rows:
        browser, os_name = parse_user_agent(row.last_user_agent)
        location = lookup_ip_location(row.last_ip, locale=locale)
        result.append(
            {
                "id": row.id,
                "browser": browser,
                "os": os_name,
                "ip": row.last_ip or "—",
                "location": location.label if location else None,
                "last_seen_at": row.last_seen_at,
                "created_at": row.created_at,
                "current": bool(current_token and row.session_token == current_token),
            }
        )
    return result
