from __future__ import annotations

import hmac
import os
import secrets
import tempfile
from datetime import datetime, timedelta

from sqlalchemy import text

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None

from flask import abort, request, session

from app.core.config import _env_bool
from app.core.extensions import db
from app.domain.models import AuthThrottle


_DB_SCHEMA_READY = False


def _ensure_supporting_indexes() -> None:
    if db.engine.dialect.name != "sqlite":
        return
    statements = (
        "CREATE INDEX IF NOT EXISTS ix_subscription_user_id ON subscription (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_subscription_user_active_expiry ON subscription (user_id, is_active, expiry_date)",
        "CREATE INDEX IF NOT EXISTS ix_payment_intent_user_processed_plan ON payment_intent (user_id, processed_at, plan_months)",
        "CREATE INDEX IF NOT EXISTS ix_referral_signup_referrer_created ON referral_signup (referrer_user_id, created_at)",
    )
    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(text("PRAGMA optimize"))


def get_webhook_secret(env_name: str) -> str:
    return (os.environ.get(env_name) or "").strip()


def ensure_db_schema() -> None:
    global _DB_SCHEMA_READY
    if _DB_SCHEMA_READY:
        return
    try:
        if fcntl is not None:
            lock_path = os.environ.get("DB_SCHEMA_LOCK_FILE", os.path.join(tempfile.gettempdir(), "vexnd_db_schema.lock"))
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            with open(lock_path, "w") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                db.create_all()
                _ensure_supporting_indexes()
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        else:
            db.create_all()
            _ensure_supporting_indexes()
        _DB_SCHEMA_READY = True
    except Exception as exc:
        print(f"DB schema ensure failed: {exc}")


def client_ip() -> str:
    if _env_bool("TRUST_PROXY_HEADERS", False):
        xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if xff:
            return xff
    return request.remote_addr or "unknown"


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def rotate_csrf_token() -> str:
    token = secrets.token_urlsafe(32)
    session["_csrf_token"] = token
    return token


def renew_session(*, preserve_keys: tuple[str, ...] = ()) -> str:
    preserved = {key: session[key] for key in preserve_keys if key in session}
    session.clear()
    session.update(preserved)
    return rotate_csrf_token()


def require_csrf() -> None:
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    expected = session.get("_csrf_token")
    if not sent or not expected or not hmac.compare_digest(str(sent), str(expected)):
        abort(400)


def throttle_get(action: str, key: str) -> AuthThrottle:
    rec = AuthThrottle.query.filter_by(action=action, key=key).first()
    if rec:
        return rec
    rec = AuthThrottle(action=action, key=key)
    db.session.add(rec)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return rec


def throttle_is_locked(action: str, key: str) -> tuple[bool, int]:
    rec = AuthThrottle.query.filter_by(action=action, key=key).first()
    if not rec or not rec.locked_until:
        return (False, 0)
    now = datetime.utcnow()
    if rec.locked_until <= now:
        rec.locked_until = None
        rec.fails = 0
        rec.first_seen = now
        rec.last_seen = now
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return (False, 0)
    return (True, int((rec.locked_until - now).total_seconds()))


def throttle_register_fail(action: str, key: str, *, window_seconds: int, max_fails: int, lock_seconds: int) -> None:
    now = datetime.utcnow()
    rec = throttle_get(action, key)
    if (now - rec.first_seen).total_seconds() > window_seconds:
        rec.first_seen = now
        rec.fails = 0
    rec.last_seen = now
    rec.fails = int(rec.fails or 0) + 1
    if rec.fails >= max_fails:
        rec.locked_until = now + timedelta(seconds=lock_seconds)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def throttle_reset(action: str, key: str) -> None:
    rec = AuthThrottle.query.filter_by(action=action, key=key).first()
    if not rec:
        return
    now = datetime.utcnow()
    rec.fails = 0
    rec.first_seen = now
    rec.last_seen = now
    rec.locked_until = None
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def validate_password_strength(password: str) -> bool:
    if not password or len(password) < 10:
        return False
    has_lower = any(ch.islower() for ch in password)
    has_upper = any(ch.isupper() for ch in password)
    has_digit = any(ch.isdigit() for ch in password)
    has_other = any((not ch.isalnum()) for ch in password)
    return sum([has_lower, has_upper, has_digit, has_other]) >= 3


def device_fingerprint(ip: str | None, user_agent: str | None) -> str:
    import hashlib

    raw = f"{(ip or '').strip()}|{(user_agent or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "client_ip",
    "csrf_token",
    "device_fingerprint",
    "ensure_db_schema",
    "get_webhook_secret",
    "require_csrf",
    "renew_session",
    "rotate_csrf_token",
    "throttle_get",
    "throttle_is_locked",
    "throttle_register_fail",
    "throttle_reset",
    "validate_password_strength",
]
