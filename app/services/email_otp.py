from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

from flask import current_app
from werkzeug.security import generate_password_hash

from app.core.config import HTTP
from app.core.extensions import db
from app.domain.models import PendingRegistration


OTP_LENGTH = 6
OTP_TTL_MINUTES = int(os.environ.get("EMAIL_OTP_TTL_MINUTES", "10"))
OTP_MAX_ATTEMPTS = int(os.environ.get("EMAIL_OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_COOLDOWN_SECONDS = int(os.environ.get("EMAIL_OTP_RESEND_COOLDOWN_SECONDS", "60"))


class EmailOtpError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.utcnow()


def _otp_hash(email: str, otp_code: str) -> str:
    secret = (current_app.config.get("SECRET_KEY") or "").strip()
    raw = f"{email.strip().lower()}:{otp_code.strip()}:{secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_otp_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))


def _resend_sender() -> str:
    from_email = (os.environ.get("RESEND_FROM_EMAIL") or "onboarding@resend.dev").strip()
    from_name = (os.environ.get("RESEND_FROM_NAME") or "VEXND").strip()
    return f"{from_name} <{from_email}>" if from_name else from_email


def _send_resend_email(*, to_email: str, subject: str, html: str, text: str) -> None:
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not api_key:
        raise EmailOtpError("RESEND_API_KEY is not configured")
    response = HTTP.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": _resend_sender(),
            "to": [to_email],
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=15,
    )
    if response.status_code >= 400:
        raise EmailOtpError(f"Resend returned {response.status_code}: {response.text[:200]}")


def _email_content(email: str, otp_code: str, lang: str) -> tuple[str, str, str]:
    if lang == "ru":
        subject = "Подтверждение email для VEXND"
        text = (
            f"Ваш код подтверждения VEXND: {otp_code}\n\n"
            f"Код действует {OTP_TTL_MINUTES} минут. Если вы не регистрировались, просто проигнорируйте это письмо."
        )
        html = (
            "<div style=\"font-family:Arial,sans-serif;line-height:1.5;color:#111827\">"
            "<h2 style=\"margin:0 0 16px\">Подтверждение email</h2>"
            f"<p>Используйте этот код для завершения регистрации в VEXND:</p>"
            f"<div style=\"margin:20px 0;font-size:32px;font-weight:700;letter-spacing:6px\">{otp_code}</div>"
            f"<p>Код действует {OTP_TTL_MINUTES} минут.</p>"
            "<p>Если вы не регистрировались, просто проигнорируйте это письмо.</p>"
            f"<p style=\"color:#6b7280;font-size:12px\">Получатель: {email}</p>"
            "</div>"
        )
        return subject, html, text
    subject = "Confirm your email for VEXND"
    text = (
        f"Your VEXND verification code is: {otp_code}\n\n"
        f"This code expires in {OTP_TTL_MINUTES} minutes. If this was not you, you can ignore this email."
    )
    html = (
        "<div style=\"font-family:Arial,sans-serif;line-height:1.5;color:#111827\">"
        "<h2 style=\"margin:0 0 16px\">Email confirmation</h2>"
        "<p>Use this code to finish creating your VEXND account:</p>"
        f"<div style=\"margin:20px 0;font-size:32px;font-weight:700;letter-spacing:6px\">{otp_code}</div>"
        f"<p>This code expires in {OTP_TTL_MINUTES} minutes.</p>"
        "<p>If this was not you, you can ignore this email.</p>"
        f"<p style=\"color:#6b7280;font-size:12px\">Recipient: {email}</p>"
        "</div>"
    )
    return subject, html, text


def get_pending_registration(email: str | None) -> PendingRegistration | None:
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    return PendingRegistration.query.filter_by(email=normalized).first()


def cleanup_expired_pending_registrations() -> None:
    PendingRegistration.query.filter(PendingRegistration.otp_expires_at < _utcnow()).delete(synchronize_session=False)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def start_pending_registration(
    *,
    email: str,
    password: str,
    lang: str,
    referral_code: str | None = None,
    referral_fingerprint: str | None = None,
) -> PendingRegistration:
    normalized_email = email.strip().lower()
    cleanup_expired_pending_registrations()
    otp_code = _generate_otp_code()
    now = _utcnow()
    record = PendingRegistration.query.filter_by(email=normalized_email).first()
    if not record:
        record = PendingRegistration(email=normalized_email)
        db.session.add(record)
    record.password_hash = generate_password_hash(password, method="pbkdf2:sha256:120000")
    record.lang = lang if lang in ("ru", "en") else "en"
    record.otp_code_hash = _otp_hash(normalized_email, otp_code)
    record.otp_expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
    record.otp_attempts = 0
    record.send_count = int(record.send_count or 0) + 1
    record.last_sent_at = now
    record.referral_code = (referral_code or "").strip().upper() or None
    record.referral_fingerprint = (referral_fingerprint or "").strip() or None
    record.updated_at = now
    subject, html, text = _email_content(normalized_email, otp_code, record.lang)
    try:
        _send_resend_email(to_email=normalized_email, subject=subject, html=html, text=text)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return record


def resend_pending_registration(email: str) -> PendingRegistration:
    record = get_pending_registration(email)
    if not record:
        raise EmailOtpError("pending registration not found")
    now = _utcnow()
    if record.last_sent_at and (now - record.last_sent_at).total_seconds() < OTP_RESEND_COOLDOWN_SECONDS:
        raise EmailOtpError("cooldown")
    otp_code = _generate_otp_code()
    record.otp_code_hash = _otp_hash(record.email, otp_code)
    record.otp_expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
    record.otp_attempts = 0
    record.send_count = int(record.send_count or 0) + 1
    record.last_sent_at = now
    record.updated_at = now
    subject, html, text = _email_content(record.email, otp_code, record.lang)
    try:
        _send_resend_email(to_email=record.email, subject=subject, html=html, text=text)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return record


def verify_pending_registration(email: str, otp_code: str) -> tuple[bool, str, PendingRegistration | None]:
    record = get_pending_registration(email)
    if not record:
        return False, "not_found", None
    now = _utcnow()
    if record.otp_expires_at <= now:
        return False, "expired", record
    if int(record.otp_attempts or 0) >= OTP_MAX_ATTEMPTS:
        return False, "too_many_attempts", record
    if not hmac.compare_digest(record.otp_code_hash, _otp_hash(record.email, otp_code)):
        record.otp_attempts = int(record.otp_attempts or 0) + 1
        record.updated_at = now
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        if int(record.otp_attempts or 0) >= OTP_MAX_ATTEMPTS:
            return False, "too_many_attempts", record
        return False, "invalid_code", record
    return True, "ok", record


def delete_pending_registration(record: PendingRegistration | None) -> None:
    if not record:
        return
    db.session.delete(record)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
