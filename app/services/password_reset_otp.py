from __future__ import annotations

import hmac
from datetime import datetime, timedelta

from app.bot.common import h, send_message
from app.bot.models import BotUserState, TelegramAccount
from app.core.extensions import db
from app.domain.models import PendingPasswordReset, User
from app.services.email_otp import (
    OTP_MAX_ATTEMPTS,
    OTP_RESEND_COOLDOWN_SECONDS,
    OTP_TTL_MINUTES,
    EmailOtpError,
    _generate_otp_code,
    _otp_hash,
    _send_resend_email,
)
from app.services.remnawave import is_telegram_placeholder_email


def _utcnow() -> datetime:
    return datetime.utcnow()


def _password_reset_content(email: str, otp_code: str, lang: str) -> tuple[str, str, str]:
    if lang == "ru":
        subject = "Сброс пароля для VEXND"
        text = (
            f"Ваш код для сброса пароля VEXND: {otp_code}\n\n"
            f"Код действует {OTP_TTL_MINUTES} минут. Если это были не вы, просто проигнорируйте письмо."
        )
        html = (
            "<div style=\"font-family:Arial,sans-serif;line-height:1.5;color:#111827\">"
            "<h2 style=\"margin:0 0 16px\">Сброс пароля</h2>"
            "<p>Используйте этот код, чтобы задать новый пароль для аккаунта VEXND:</p>"
            f"<div style=\"margin:20px 0;font-size:32px;font-weight:700;letter-spacing:6px\">{otp_code}</div>"
            f"<p>Код действует {OTP_TTL_MINUTES} минут.</p>"
            "<p>Если это были не вы, просто проигнорируйте письмо.</p>"
            f"<p style=\"color:#6b7280;font-size:12px\">Email: {email}</p>"
            "</div>"
        )
        return subject, html, text
    subject = "Reset your VEXND password"
    text = (
        f"Your VEXND password reset code is: {otp_code}\n\n"
        f"This code expires in {OTP_TTL_MINUTES} minutes. If this was not you, you can ignore this email."
    )
    html = (
        "<div style=\"font-family:Arial,sans-serif;line-height:1.5;color:#111827\">"
        "<h2 style=\"margin:0 0 16px\">Password reset</h2>"
        "<p>Use this code to set a new password for your VEXND account:</p>"
        f"<div style=\"margin:20px 0;font-size:32px;font-weight:700;letter-spacing:6px\">{otp_code}</div>"
        f"<p>This code expires in {OTP_TTL_MINUTES} minutes.</p>"
        "<p>If this was not you, you can ignore this email.</p>"
        f"<p style=\"color:#6b7280;font-size:12px\">Email: {email}</p>"
        "</div>"
    )
    return subject, html, text


def _password_reset_telegram_text(otp_code: str, lang: str) -> str:
    if lang == "ru":
        return (
            "🔑 <b>Сброс пароля</b>\n\n"
            "Ваш код для сброса пароля на сайте VEXND:\n"
            f"<code>{h(otp_code)}</code>\n\n"
            f"Код действует {OTP_TTL_MINUTES} минут.\n"
            "Введите его на сайте, чтобы задать новый пароль."
        )
    return (
        "🔑 <b>Password reset</b>\n\n"
        "Your code to reset your VEXND website password:\n"
        f"<code>{h(otp_code)}</code>\n\n"
        f"This code expires in {OTP_TTL_MINUTES} minutes.\n"
        "Enter it on the website to set a new password."
    )


def password_reset_uses_telegram(email: str | None) -> bool:
    return is_telegram_placeholder_email((email or "").strip().lower())


def _send_password_reset_via_telegram(email: str, otp_code: str) -> None:
    user = User.query.filter_by(email=(email or "").strip().lower()).first()
    if not user:
        raise EmailOtpError("user_not_found")
    telegram_account = TelegramAccount.query.filter_by(user_id=user.id).first()
    if not telegram_account:
        raise EmailOtpError("telegram_not_linked")
    state = BotUserState.query.filter_by(telegram_id=telegram_account.telegram_id).first()
    lang = state.lang if state and state.lang in ("ru", "en") else (user.lang if user.lang in ("ru", "en") else "en")
    send_message(telegram_account.telegram_id, _password_reset_telegram_text(otp_code, lang))


def get_pending_password_reset(email: str | None) -> PendingPasswordReset | None:
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    return PendingPasswordReset.query.filter_by(email=normalized).first()


def cleanup_expired_pending_password_resets() -> None:
    PendingPasswordReset.query.filter(PendingPasswordReset.otp_expires_at < _utcnow()).delete(synchronize_session=False)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def start_pending_password_reset(*, email: str, lang: str) -> PendingPasswordReset:
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        raise EmailOtpError("empty_email")
    user = User.query.filter_by(email=normalized_email).first()
    if not user:
        raise EmailOtpError("user_not_found")

    cleanup_expired_pending_password_resets()

    otp_code = _generate_otp_code()
    now = _utcnow()
    record = get_pending_password_reset(normalized_email)
    if not record:
        record = PendingPasswordReset(email=normalized_email)
        db.session.add(record)

    record.otp_code_hash = _otp_hash(normalized_email, otp_code)
    record.otp_expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
    record.otp_attempts = 0
    record.send_count = int(record.send_count or 0) + 1
    record.last_sent_at = now
    record.updated_at = now

    try:
        if password_reset_uses_telegram(normalized_email):
            _send_password_reset_via_telegram(normalized_email, otp_code)
        else:
            subject, html, text = _password_reset_content(normalized_email, otp_code, lang if lang in ("ru", "en") else "en")
            _send_resend_email(to_email=normalized_email, subject=subject, html=html, text=text)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return record


def resend_pending_password_reset(email: str) -> PendingPasswordReset:
    record = get_pending_password_reset(email)
    if not record:
        raise EmailOtpError("not_found")

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

    try:
        if password_reset_uses_telegram(record.email):
            _send_password_reset_via_telegram(record.email, otp_code)
        else:
            user = User.query.filter_by(email=record.email).first()
            lang = user.lang if user and user.lang in ("ru", "en") else "en"
            subject, html, text = _password_reset_content(record.email, otp_code, lang)
            _send_resend_email(to_email=record.email, subject=subject, html=html, text=text)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return record


def verify_pending_password_reset(email: str, otp_code: str) -> tuple[bool, str, PendingPasswordReset | None]:
    record = get_pending_password_reset(email)
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


def delete_pending_password_reset(record: PendingPasswordReset | None) -> None:
    if not record:
        return
    db.session.delete(record)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
