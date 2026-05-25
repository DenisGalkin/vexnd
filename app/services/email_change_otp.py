from __future__ import annotations

import hmac
from datetime import datetime, timedelta

from app.core.extensions import db
from app.domain.models import PendingEmailChange, User
from app.services.email_otp import (
    OTP_MAX_ATTEMPTS,
    OTP_RESEND_COOLDOWN_SECONDS,
    OTP_TTL_MINUTES,
    EmailOtpError,
    _generate_otp_code,
    _otp_hash,
    _send_resend_email,
)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _email_change_content(email: str, otp_code: str, lang: str) -> tuple[str, str, str]:
    if lang == "ru":
        subject = "Подтверждение смены email для VEXND"
        text = (
            f"Ваш код для смены email в VEXND: {otp_code}\n\n"
            f"Код действует {OTP_TTL_MINUTES} минут. Если это были не вы, просто проигнорируйте письмо."
        )
        html = (
            "<div style=\"font-family:Arial,sans-serif;line-height:1.5;color:#111827\">"
            "<h2 style=\"margin:0 0 16px\">Смена email</h2>"
            "<p>Используйте этот код, чтобы подтвердить новый email для аккаунта VEXND:</p>"
            f"<div style=\"margin:20px 0;font-size:32px;font-weight:700;letter-spacing:6px\">{otp_code}</div>"
            f"<p>Код действует {OTP_TTL_MINUTES} минут.</p>"
            "<p>Если это были не вы, просто проигнорируйте письмо.</p>"
            f"<p style=\"color:#6b7280;font-size:12px\">Новый email: {email}</p>"
            "</div>"
        )
        return subject, html, text
    subject = "Confirm your new email for VEXND"
    text = (
        f"Your VEXND email change code is: {otp_code}\n\n"
        f"This code expires in {OTP_TTL_MINUTES} minutes. If this was not you, you can ignore this email."
    )
    html = (
        "<div style=\"font-family:Arial,sans-serif;line-height:1.5;color:#111827\">"
        "<h2 style=\"margin:0 0 16px\">Change email</h2>"
        "<p>Use this code to confirm your new email for your VEXND account:</p>"
        f"<div style=\"margin:20px 0;font-size:32px;font-weight:700;letter-spacing:6px\">{otp_code}</div>"
        f"<p>This code expires in {OTP_TTL_MINUTES} minutes.</p>"
        "<p>If this was not you, you can ignore this email.</p>"
        f"<p style=\"color:#6b7280;font-size:12px\">New email: {email}</p>"
        "</div>"
    )
    return subject, html, text


def get_pending_email_change(user_id: int | None) -> PendingEmailChange | None:
    if not user_id:
        return None
    return PendingEmailChange.query.filter_by(user_id=int(user_id)).first()


def cleanup_expired_pending_email_changes() -> None:
    PendingEmailChange.query.filter(PendingEmailChange.otp_expires_at < _utcnow()).delete(synchronize_session=False)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def start_pending_email_change(*, user: User, new_email: str, lang: str) -> PendingEmailChange:
    normalized_email = (new_email or "").strip().lower()
    if not normalized_email:
        raise EmailOtpError("empty_email")
    if (user.email or "").strip().lower() == normalized_email:
        raise EmailOtpError("same_email")
    if User.query.filter_by(email=normalized_email).first():
        raise EmailOtpError("email_exists")

    cleanup_expired_pending_email_changes()

    existing_email_request = PendingEmailChange.query.filter_by(new_email=normalized_email).first()
    if existing_email_request and int(existing_email_request.user_id) != int(user.id):
        raise EmailOtpError("email_already_pending")

    otp_code = _generate_otp_code()
    now = _utcnow()
    record = get_pending_email_change(user.id)
    if not record:
        record = PendingEmailChange(user_id=user.id)
        db.session.add(record)

    record.new_email = normalized_email
    record.otp_code_hash = _otp_hash(normalized_email, otp_code)
    record.otp_expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
    record.otp_attempts = 0
    record.send_count = int(record.send_count or 0) + 1
    record.last_sent_at = now
    record.updated_at = now

    subject, html, text = _email_change_content(normalized_email, otp_code, lang if lang in ("ru", "en") else "en")
    try:
        _send_resend_email(to_email=normalized_email, subject=subject, html=html, text=text)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return record


def resend_pending_email_change(user_id: int) -> PendingEmailChange:
    record = get_pending_email_change(user_id)
    if not record:
        raise EmailOtpError("not_found")

    now = _utcnow()
    if record.last_sent_at and (now - record.last_sent_at).total_seconds() < OTP_RESEND_COOLDOWN_SECONDS:
        raise EmailOtpError("cooldown")

    otp_code = _generate_otp_code()
    record.otp_code_hash = _otp_hash(record.new_email, otp_code)
    record.otp_expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
    record.otp_attempts = 0
    record.send_count = int(record.send_count or 0) + 1
    record.last_sent_at = now
    record.updated_at = now

    user = db.session.get(User, int(record.user_id))
    lang = (user.lang if user and user.lang in ("ru", "en") else "en")
    subject, html, text = _email_change_content(record.new_email, otp_code, lang if lang in ("ru", "en") else "en")
    try:
        _send_resend_email(to_email=record.new_email, subject=subject, html=html, text=text)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return record


def verify_pending_email_change(user_id: int, otp_code: str) -> tuple[bool, str, PendingEmailChange | None]:
    record = get_pending_email_change(user_id)
    if not record:
        return False, "not_found", None

    now = _utcnow()
    if record.otp_expires_at <= now:
        return False, "expired", record
    if int(record.otp_attempts or 0) >= OTP_MAX_ATTEMPTS:
        return False, "too_many_attempts", record
    if not hmac.compare_digest(record.otp_code_hash, _otp_hash(record.new_email, otp_code)):
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


def delete_pending_email_change(record: PendingEmailChange | None) -> None:
    if not record:
        return
    db.session.delete(record)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
