from __future__ import annotations

from datetime import datetime

from flask import current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from email_validator import EmailNotValidError, validate_email
from werkzeug.security import check_password_hash

from app.bot.models import TelegramAccount
from app.core.extensions import db
from app.services.account_deletion import delete_user_account
from app.domain.models import PendingRegistration, ReferralCode, ReferralFingerprint, ReferralSignup, User, UserSecurity
from app.services.email_change_otp import (
    delete_pending_email_change,
    get_pending_email_change,
    resend_pending_email_change,
    start_pending_email_change,
    verify_pending_email_change,
)
from app.services.email_otp import OTP_RESEND_COOLDOWN_SECONDS, OTP_TTL_MINUTES, EmailOtpError, delete_pending_registration, get_pending_registration, resend_pending_registration, start_pending_registration, verify_pending_registration
from app.services.password_reset_otp import (
    delete_pending_password_reset,
    get_pending_password_reset,
    password_reset_uses_telegram,
    resend_pending_password_reset,
    start_pending_password_reset,
    verify_pending_password_reset,
)
from app.services.referrals import mask_email
from app.services.remnawave import get_remnawave_config, is_telegram_placeholder_email, remnawave_sync_user_identity
from app.services.security import client_ip, device_fingerprint, renew_session, throttle_is_locked, throttle_register_fail, throttle_reset, validate_password_strength
from app.services.telegram_auth import CHALLENGE_TTL_MINUTES, consume_approved_challenge, create_telegram_auth_challenge, get_active_challenge
from app.services.telegram_links import telegram_bot_deeplink
from app.services.web_sessions import current_web_session_token, revoke_current_web_session, revoke_other_web_sessions, revoke_user_web_session
from app.http.helpers import get_locale, localized_url, redirect_localized, translate
from app.http.routes.dashboard import _dashboard_security_context


def _normalized_lang() -> str:
    chosen = session.get("lang") or get_locale()
    return chosen if chosen in ("ru", "en") else "en"


def _telegram_session_key(purpose: str) -> str:
    return f"tg_auth_code:{purpose}"


def _telegram_challenge_context(*, purpose: str, target_user_id: int | None = None) -> dict[str, str | int | None]:
    session_key = _telegram_session_key(purpose)
    challenge = get_active_challenge(session.get(session_key), purpose=purpose)
    if challenge and target_user_id is not None and challenge.target_user_id != target_user_id:
        challenge = None
    if not challenge:
        challenge = create_telegram_auth_challenge(purpose=purpose, target_user_id=target_user_id)
        session[session_key] = challenge.code
    start_value = f"{purpose}_{challenge.code}"
    return {
        "code": challenge.code,
        "minutes": CHALLENGE_TTL_MINUTES,
        "bot_url": telegram_bot_deeplink(start_value),
        "status_url": url_for("telegram_auth_status", code=challenge.code),
        "command": f"/start {start_value}",
    }


def _pending_registration_session_key() -> str:
    return "pending_registration_email"


def _clear_pending_registration_session() -> None:
    session.pop(_pending_registration_session_key(), None)


def _store_pending_registration_session(email: str) -> None:
    session[_pending_registration_session_key()] = email.strip().lower()


def _pending_registration_context() -> dict[str, object]:
    pending_email = (session.get(_pending_registration_session_key()) or "").strip().lower()
    pending = get_pending_registration(pending_email)
    if not pending:
        _clear_pending_registration_session()
        return {"verification_pending": False}
    return {
        "verification_pending": True,
        "pending_email": pending.email,
        "masked_pending_email": mask_email(pending.email),
        "otp_ttl_minutes": OTP_TTL_MINUTES,
        "resend_cooldown_seconds": OTP_RESEND_COOLDOWN_SECONDS,
    }


def _pending_password_reset_session_key() -> str:
    return "pending_password_reset_email"


def _clear_pending_password_reset_session() -> None:
    session.pop(_pending_password_reset_session_key(), None)


def _store_pending_password_reset_session(email: str) -> None:
    session[_pending_password_reset_session_key()] = email.strip().lower()


def _pending_password_reset_context() -> dict[str, object]:
    pending_email = (session.get(_pending_password_reset_session_key()) or "").strip().lower()
    pending = get_pending_password_reset(pending_email)
    if not pending:
        _clear_pending_password_reset_session()
        return {"password_reset_pending": False}
    return {
        "password_reset_pending": True,
        "password_reset_email": pending.email,
        "password_reset_email_masked": mask_email(pending.email),
        "otp_ttl_minutes": OTP_TTL_MINUTES,
        "resend_cooldown_seconds": OTP_RESEND_COOLDOWN_SECONDS,
    }


def _settings_redirect():
    return redirect(localized_url("dashboard", tab="settings"))


def _request_payload() -> dict[str, object]:
    if request.is_json:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            return payload
    return request.form.to_dict()


def _wants_json() -> bool:
    return request.is_json or request.path.startswith("/api/")


def _wants_partial_update() -> bool:
    return request.headers.get("X-Partial-Update") == "password-reset-panel"


def _respond_success(message: str, *, redirect_to: str | None = None, extra: dict[str, object] | None = None):
    if _wants_json():
        data: dict[str, object] = {"ok": True, "message": message}
        if redirect_to:
            data["redirect"] = redirect_to
        if extra:
            data.update(extra)
        return jsonify(data)
    if message:
        flash(message, "success")
    return redirect(redirect_to) if redirect_to else _settings_redirect()


def _respond_error(message: str, status_code: int = 400, *, category: str = "error", extra: dict[str, object] | None = None):
    if _wants_json():
        data: dict[str, object] = {"ok": False, "error": message}
        if extra:
            data.update(extra)
        return jsonify(data), status_code
    if message:
        flash(message, category)
    return _settings_redirect()


def _render_password_reset_panel() -> str:
    return render_template("account/_password_reset_panel.html", **_dashboard_security_context())


def _respond_password_reset_panel(message: str, *, category: str = "success", status_code: int = 200):
    if _wants_partial_update():
        return jsonify(
            {
                "ok": status_code < 400,
                "message": message,
                "category": category,
                "html": _render_password_reset_panel(),
            }
        ), status_code
    if status_code >= 400:
        return _respond_error(message, status_code, category=category)
    return _respond_success(message)


def _normalize_email_input(value: str | None) -> str:
    normalized = validate_email((value or "").strip(), check_deliverability=False).normalized
    return normalized.strip().lower()


def _password_is_valid(user: User, password: str | None) -> bool:
    return bool(user.password_hash and password and check_password_hash(user.password_hash, password))


def _sync_identity_safe(user: User) -> None:
    try:
        cfg = get_remnawave_config()
        if cfg.base_url and cfg.token:
            remnawave_sync_user_identity(cfg, user)
    except Exception:
        current_app.logger.exception("Remnawave identity sync failed")


def _complete_referral_signup(user: User, pending: PendingRegistration) -> None:
    try:
        if not pending.referral_code or not pending.referral_fingerprint:
            return
        rc = ReferralCode.query.filter_by(code=pending.referral_code).first()
        if not rc or not rc.user_id or rc.user_id == user.id:
            return
        if ReferralSignup.query.filter_by(referred_user_id=user.id).first():
            return
        if ReferralFingerprint.query.filter_by(fingerprint=pending.referral_fingerprint).first():
            return
        db.session.add(ReferralSignup(referrer_user_id=rc.user_id, referred_user_id=user.id, code_used=pending.referral_code))
        db.session.add(ReferralFingerprint(fingerprint=pending.referral_fingerprint, referred_user_id=user.id))
        db.session.commit()
    except Exception:
        current_app.logger.exception("Referral link on register confirm failed")
        try:
            db.session.rollback()
        except Exception:
            pass


def login():
    if current_user.is_authenticated:
        return redirect_localized("dashboard")
    telegram_auth = _telegram_challenge_context(purpose="login")
    template_context = {
        "telegram_auth": telegram_auth,
        **_pending_password_reset_context(),
    }
    if request.method == "POST":
        ip = client_ip()
        action = (request.form.get("action") or "login").strip().lower()
        if action in {"forgot_password", "reset_verify", "reset_resend", "reset_cancel"}:
            if action == "reset_cancel":
                pending_email = (session.get(_pending_password_reset_session_key()) or "").strip().lower()
                delete_pending_password_reset(get_pending_password_reset(pending_email))
                _clear_pending_password_reset_session()
                flash(translate("Сброс пароля отменён."), "info")
                return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
            if action == "reset_resend":
                pending_email = (session.get(_pending_password_reset_session_key()) or "").strip().lower()
                if not pending_email:
                    flash(translate("Сессия сброса пароля истекла. Запросите новый код."), "error")
                    return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
                try:
                    resend_pending_password_reset(pending_email)
                except EmailOtpError as exc:
                    if str(exc) == "cooldown":
                        flash(translate("Код уже отправлен. Подождите немного перед повторной отправкой."), "error")
                    elif str(exc) == "not_found":
                        _clear_pending_password_reset_session()
                        flash(translate("Сессия сброса пароля истекла. Запросите новый код."), "error")
                    else:
                        current_app.logger.exception("Resend password reset OTP failed")
                        flash(translate("Не удалось отправить код подтверждения. Попробуйте ещё раз позже."), "error")
                else:
                    flash(translate("Новый код для сброса пароля отправлен на email."), "success")
                return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
            if action == "reset_verify":
                pending_email = (session.get(_pending_password_reset_session_key()) or "").strip().lower()
                otp_code = (request.form.get("otp_code") or "").strip()
                new_password = request.form.get("new_password") or ""
                new_password2 = request.form.get("new_password2") or ""
                if not pending_email:
                    flash(translate("Сессия сброса пароля истекла. Запросите новый код."), "error")
                    return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
                if not otp_code.isdigit() or len(otp_code) != 6:
                    flash(translate("Введите 6-значный код из письма."), "error")
                    return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
                if new_password != new_password2:
                    flash(translate("Пароли не совпадают"), "error")
                    return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
                if not validate_password_strength(new_password):
                    flash(translate("Пароль слишком слабый. Используйте минимум 10 символов и комбинацию букв/цифр/символов."), "error")
                    return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
                ok, reason, pending = verify_pending_password_reset(pending_email, otp_code)
                if not ok or not pending:
                    if reason == "expired":
                        flash(translate("Срок действия кода истёк. Отправьте новый код."), "error")
                    elif reason == "too_many_attempts":
                        flash(translate("Слишком много неверных попыток. Запросите новый код."), "error")
                    elif reason == "not_found":
                        _clear_pending_password_reset_session()
                        flash(translate("Сессия сброса пароля истекла. Запросите новый код."), "error")
                    else:
                        flash(translate("Неверный код подтверждения."), "error")
                    return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
                user = User.query.filter_by(email=pending.email).first()
                if not user:
                    delete_pending_password_reset(pending)
                    _clear_pending_password_reset_session()
                    flash(translate("Аккаунт с таким email не найден."), "error")
                    return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
                try:
                    user.set_password(new_password)
                    db.session.commit()
                    delete_pending_password_reset(pending)
                    _clear_pending_password_reset_session()
                    throttle_reset("login", f"email:{pending.email}")
                    flash(translate("Пароль успешно обновлён. Теперь можно войти."), "success")
                    return redirect_localized("login")
                except Exception:
                    db.session.rollback()
                    flash(translate("Не удалось изменить пароль. Попробуйте позже."), "error")
                    return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
            email_raw = request.form.get("email")
            try:
                email = _normalize_email_input(email_raw)
            except EmailNotValidError:
                flash(translate("Введите корректный email"), "error")
                return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
            try:
                start_pending_password_reset(email=email, lang=_normalized_lang())
                _store_pending_password_reset_session(email)
                flash(translate("Мы отправили код для сброса пароля на ваш email."), "success")
            except EmailOtpError as exc:
                if str(exc) == "user_not_found":
                    flash(translate("Аккаунт с таким email не найден."), "error")
                else:
                    current_app.logger.exception("Start password reset failed")
                    flash(translate("Не удалось отправить код подтверждения. Попробуйте ещё раз позже."), "error")
            return render_template("auth/login.html", telegram_auth=telegram_auth, **_pending_password_reset_context())
        email = request.form.get("email")
        password = request.form.get("password")
        remember = True if request.form.get("remember_me") else False
        if email:
            locked, _ = throttle_is_locked("login", f"email:{email.lower().strip()}")
            if locked:
                flash(translate("Слишком много попыток входа. Попробуйте позже."), "error")
                return render_template("auth/login.html", **template_context), 429
        locked, _ = throttle_is_locked("login", f"ip:{ip}")
        if locked:
            flash(translate("Слишком много попыток входа. Попробуйте позже."), "error")
            return render_template("auth/login.html", **template_context), 429
        user = User.query.filter_by(email=email.lower().strip() if email else email).first()
        if user and user.check_password(password):
            chosen = _normalized_lang()
            renew_session(preserve_keys=("lang",))
            login_user(user, remember=remember)
            session["lang"] = chosen
            try:
                user.lang = chosen
                db.session.commit()
            except Exception:
                pass
            if email:
                throttle_reset("login", f"email:{email.lower().strip()}")
            throttle_reset("login", f"ip:{ip}")
            return redirect_localized("dashboard")
        flash(translate("Неверный email или пароль"), "error")
        if email:
            throttle_register_fail("login", f"email:{email.lower().strip()}", window_seconds=15 * 60, max_fails=6, lock_seconds=15 * 60)
        throttle_register_fail("login", f"ip:{ip}", window_seconds=15 * 60, max_fails=12, lock_seconds=15 * 60)
    return render_template("auth/login.html", **template_context)


def referral(code):
    if current_user.is_authenticated:
        return redirect_localized("dashboard")
    code = (code or "").strip().upper()
    rc = ReferralCode.query.filter_by(code=code).first()
    if not rc:
        flash(translate("Реферальная ссылка недействительна."), "error")
        return redirect_localized("index")
    if not (session.get("ref_code") or "").strip():
        session["ref_code"] = code
        session["ref_code_set_at"] = datetime.utcnow().isoformat()
        try:
            ref_user = User.query.get(rc.user_id)
            if ref_user:
                flash(translate("Вы перешли по реферальной ссылке. После первой оплаты вы получите +3 дня, а пригласивший — +5 дней."), "info")
        except Exception:
            pass
    return redirect("/en/register" if request.path.startswith("/en/") else localized_url("register"))


def register_user():
    if current_user.is_authenticated:
        return redirect_localized("dashboard")
    rc = None
    referrer_masked = None
    referral_ok = True
    referrer_id = None
    ref_q = (request.args.get("ref") or "").strip().upper()
    if ref_q and not (session.get("ref_code") or "").strip():
        try:
            rc = ReferralCode.query.filter_by(code=ref_q).first()
        except Exception:
            rc = None
        if rc:
            session["ref_code"] = ref_q
            session["ref_code_set_at"] = datetime.utcnow().isoformat()
    ref_code = (session.get("ref_code") or "").strip().upper()
    if ref_code:
        try:
            if not (rc and getattr(rc, "code", None) == ref_code):
                rc = ReferralCode.query.filter_by(code=ref_code).first()
        except Exception:
            rc = None
        if not rc:
            session.pop("ref_code", None)
            session.pop("ref_code_set_at", None)
            ref_code = ""
        else:
            referrer_id = rc.user_id
            ref_user = User.query.get(referrer_id)
            if ref_user:
                referrer_masked = mask_email(ref_user.email)
            try:
                ip = client_ip()
                ua = (request.headers.get("User-Agent", "") or "")[:250]
                fp = device_fingerprint(ip, ua)
                ref_sec = UserSecurity.query.filter_by(user_id=referrer_id).first()
                if ref_sec and ref_sec.last_fingerprint and ref_sec.last_fingerprint == fp:
                    referral_ok = False
                if ReferralFingerprint.query.filter_by(fingerprint=fp).first():
                    referral_ok = False
                if not referral_ok:
                    session.pop("ref_code", None)
                    session.pop("ref_code_set_at", None)
                    referrer_masked = None
                    referrer_id = None
            except Exception:
                session.pop("ref_code", None)
                session.pop("ref_code_set_at", None)
                referrer_masked = None
                referrer_id = None
                referral_ok = False
    template_context = {
        "referrer_email": referrer_masked,
        **_pending_registration_context(),
    }
    if request.method == "POST":
        ip = client_ip()
        action = (request.form.get("action") or "start").strip().lower()
        if action == "resend":
            pending_email = (session.get(_pending_registration_session_key()) or "").strip().lower()
            if not pending_email:
                flash(translate("Сессия подтверждения истекла. Заполните форму регистрации ещё раз."), "error")
                return render_template("auth/register.html", **template_context)
            try:
                resend_pending_registration(pending_email)
            except EmailOtpError as exc:
                if str(exc) == "cooldown":
                    flash(translate("Код уже отправлен. Подождите немного перед повторной отправкой."), "error")
                else:
                    current_app.logger.exception("Resend OTP failed")
                    flash(translate("Не удалось отправить код подтверждения. Попробуйте ещё раз позже."), "error")
            else:
                flash(translate("Новый код подтверждения отправлен на email."), "success")
            return render_template("auth/register.html", referrer_email=referrer_masked, **_pending_registration_context())
        if action == "verify":
            pending_email = (session.get(_pending_registration_session_key()) or "").strip().lower()
            otp_code = (request.form.get("otp_code") or "").strip()
            if not pending_email:
                flash(translate("Сессия подтверждения истекла. Заполните форму регистрации ещё раз."), "error")
                return render_template("auth/register.html", **template_context)
            if not otp_code or not otp_code.isdigit() or len(otp_code) != 6:
                flash(translate("Введите 6-значный код из письма."), "error")
                return render_template("auth/register.html", referrer_email=referrer_masked, **_pending_registration_context())
            ok, reason, pending = verify_pending_registration(pending_email, otp_code)
            if not ok or not pending:
                if reason == "expired":
                    flash(translate("Срок действия кода истёк. Отправьте новый код."), "error")
                elif reason == "too_many_attempts":
                    flash(translate("Слишком много неверных попыток. Запросите новый код."), "error")
                elif reason == "not_found":
                    _clear_pending_registration_session()
                    flash(translate("Сессия подтверждения истекла. Заполните форму регистрации ещё раз."), "error")
                else:
                    flash(translate("Неверный код подтверждения."), "error")
                return render_template("auth/register.html", referrer_email=referrer_masked, **_pending_registration_context())
            if User.query.filter_by(email=pending.email).first():
                delete_pending_registration(pending)
                _clear_pending_registration_session()
                flash(translate("Email уже зарегистрирован"), "error")
                return render_template("auth/register.html", referrer_email=referrer_masked)
            chosen_lang = pending.lang if pending.lang in ("ru", "en") else get_locale()
            user = User(email=pending.email, lang=chosen_lang)
            user.password_hash = pending.password_hash
            try:
                db.session.add(user)
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
                if User.query.filter_by(email=pending.email).first():
                    delete_pending_registration(pending)
                    _clear_pending_registration_session()
                    flash(translate("Email уже зарегистрирован"), "error")
                    return render_template("auth/register.html", referrer_email=referrer_masked)
                raise
            _complete_referral_signup(user, pending)
            delete_pending_registration(pending)
            _clear_pending_registration_session()
            session.pop("ref_code", None)
            session.pop("ref_code_set_at", None)
            renew_session(preserve_keys=("lang",))
            login_user(user, remember=True)
            session["lang"] = chosen_lang
            flash(translate("Email подтвержден. Регистрация завершена, добро пожаловать."), "success")
            throttle_reset("register", f"ip:{ip}")
            return redirect_localized("dashboard")
        email_raw = request.form.get("email")
        password = request.form.get("password")
        password2 = request.form.get("password2")
        locked, _ = throttle_is_locked("register", f"ip:{ip}")
        if locked:
            flash(translate("Слишком много попыток. Попробуйте позже."), "error")
            return render_template("auth/register.html"), 429
        email = (email_raw or "").strip().lower()
        if not email or not password:
            flash(translate("Заполните все поля"), "error")
            throttle_register_fail("register", f"ip:{ip}", window_seconds=30 * 60, max_fails=10, lock_seconds=30 * 60)
            return render_template("auth/register.html", referrer_email=referrer_masked)
        if password != password2:
            flash(translate("Пароли не совпадают"), "error")
            throttle_register_fail("register", f"ip:{ip}", window_seconds=30 * 60, max_fails=10, lock_seconds=30 * 60)
            return render_template("auth/register.html", referrer_email=referrer_masked)
        if not validate_password_strength(password):
            flash(translate("Пароль слишком слабый. Используйте минимум 10 символов и комбинацию букв/цифр/символов."), "error")
            throttle_register_fail("register", f"ip:{ip}", window_seconds=30 * 60, max_fails=10, lock_seconds=30 * 60)
            return render_template("auth/register.html", referrer_email=referrer_masked)
        if User.query.filter_by(email=email).first():
            flash(translate("Email уже зарегистрирован"), "error")
            throttle_register_fail("register", f"ip:{ip}", window_seconds=30 * 60, max_fails=10, lock_seconds=30 * 60)
            return render_template("auth/register.html", referrer_email=referrer_masked)
        chosen_lang = get_locale()
        referral_fp = None
        if ref_code and referral_ok:
            referral_fp = device_fingerprint(ip, (request.headers.get("User-Agent", "") or "")[:250])
        try:
            start_pending_registration(
                email=email,
                password=password,
                lang=chosen_lang,
                referral_code=ref_code if referral_ok else None,
                referral_fingerprint=referral_fp,
            )
        except EmailOtpError:
            current_app.logger.exception("Start pending registration failed")
            flash(translate("Не удалось отправить код подтверждения. Попробуйте ещё раз позже."), "error")
            return render_template("auth/register.html", referrer_email=referrer_masked)
        _store_pending_registration_session(email)
        flash(translate("Мы отправили 6-значный код подтверждения на ваш email."), "success")
        return render_template("auth/register.html", referrer_email=referrer_masked, **_pending_registration_context())
    return render_template("auth/register.html", **template_context)


@login_required
def logout():
    if request.method == "POST":
        from app.services.security import require_csrf

        require_csrf()
    chosen = _normalized_lang()
    revoke_current_web_session(current_user.id)
    logout_user()
    renew_session()
    session["lang"] = chosen
    return redirect(url_for("index"))


def telegram_auth_status(code: str):
    purpose = None
    for candidate in ("login", "link", "password_reset"):
        if session.get(_telegram_session_key(candidate)) == code:
            purpose = candidate
            break
    if not purpose:
        return jsonify({"status": "not_found"}), 404
    challenge = get_active_challenge(code, purpose=purpose)
    if not challenge:
        session.pop(_telegram_session_key(purpose), None)
        return jsonify({"status": "expired"}), 410
    if challenge.status_reason:
        return jsonify({"status": challenge.status_reason}), 409
    if challenge.approved_at is None:
        return jsonify({"status": "pending"})
    if purpose == "login":
        ok, reason, user = consume_approved_challenge(code, purpose="login")
        if not ok or not user:
            return jsonify({"status": reason}), 400
        chosen = _normalized_lang()
        renew_session(preserve_keys=("lang",))
        login_user(user, remember=False)
        session["lang"] = chosen
        return jsonify({"status": "ok", "redirect": localized_url("dashboard")})
    if purpose == "password_reset":
        if not current_user.is_authenticated or challenge.target_user_id != current_user.id:
            return jsonify({"status": "forbidden"}), 403
        return jsonify({"status": "ok", "redirect": localized_url("dashboard", tab="settings", pw_reset_tg=challenge.code)})
    if not current_user.is_authenticated or challenge.target_user_id != current_user.id:
        return jsonify({"status": "forbidden"}), 403
    session.pop(_telegram_session_key("link"), None)
    return jsonify({"status": "ok", "redirect": localized_url("dashboard", tab="settings")})


def telegram_auth_link():
    if not current_user.is_authenticated:
        return redirect_localized("login")
    challenge = _telegram_challenge_context(purpose="link", target_user_id=current_user.id)
    flash(translate("Откройте Telegram-бота и подтвердите привязку аккаунта."), "info")
    return redirect(f"{localized_url('dashboard')}?tab=settings&tg_link={challenge['code']}")


@login_required
def account_change_password():
    from app.services.security import require_csrf

    require_csrf()
    ip = client_ip()
    locked, _ = throttle_is_locked("change_password", f"ip:{ip}")
    if locked:
        flash(translate("Слишком много попыток. Попробуйте позже."), "error")
        return _settings_redirect()
    current_pw = request.form.get("current_password") or ""
    new_pw = request.form.get("new_password") or ""
    new_pw2 = request.form.get("new_password2") or ""
    if not _password_is_valid(current_user, current_pw):
        flash(translate("Текущий пароль неверный"), "error")
        throttle_register_fail("change_password", f"ip:{ip}", window_seconds=15 * 60, max_fails=6, lock_seconds=15 * 60)
        return _settings_redirect()
    if new_pw != new_pw2:
        flash(translate("Пароли не совпадают"), "error")
        return _settings_redirect()
    if not validate_password_strength(new_pw):
        flash(translate("Пароль слишком слабый. Используйте минимум 10 символов и комбинацию букв/цифр/символов."), "error")
        return _settings_redirect()
    try:
        current_user.set_password(new_pw)
        db.session.commit()
        throttle_reset("change_password", f"ip:{ip}")
        flash(translate("Пароль успешно изменён"), "success")
    except Exception:
        db.session.rollback()
        flash(translate("Не удалось изменить пароль. Попробуйте позже."), "error")
    return _settings_redirect()


@login_required
def account_password_reset_start():
    from app.services.security import require_csrf

    require_csrf()
    current_email = (current_user.email or "").strip().lower()
    telegram_account = TelegramAccount.query.filter_by(user_id=current_user.id).first()
    if not current_email:
        return _respond_password_reset_panel(
            translate("Сначала привяжите email или Telegram, чтобы сбрасывать пароль через код."),
            category="error",
            status_code=400,
        )
    if is_telegram_placeholder_email(current_email) and not telegram_account:
        return _respond_password_reset_panel(
            translate("Сначала привяжите Telegram, чтобы получать код для сброса пароля."),
            category="error",
            status_code=400,
        )
    try:
        start_pending_password_reset(email=current_email, lang=_normalized_lang())
        if password_reset_uses_telegram(current_email):
            return _respond_password_reset_panel(translate("Мы отправили код для сброса пароля в Telegram."))
        else:
            return _respond_password_reset_panel(translate("Мы отправили код для сброса пароля на ваш email."))
    except EmailOtpError as exc:
        reason = str(exc)
        if reason == "user_not_found":
            return _respond_password_reset_panel(
                translate("Аккаунт с таким email не найден."),
                category="error",
                status_code=400,
            )
        elif reason == "telegram_not_linked":
            return _respond_password_reset_panel(
                translate("Сначала привяжите Telegram, чтобы получать код для сброса пароля."),
                category="error",
                status_code=400,
            )
        current_app.logger.exception("Start password reset in settings failed")
        return _respond_password_reset_panel(
            translate("Не удалось отправить код подтверждения. Попробуйте ещё раз позже."),
            category="error",
            status_code=500,
        )


@login_required
def account_password_reset_resend():
    from app.services.security import require_csrf

    require_csrf()
    current_email = (current_user.email or "").strip().lower()
    try:
        resend_pending_password_reset(current_email)
        if password_reset_uses_telegram(current_email):
            return _respond_password_reset_panel(translate("Новый код для сброса пароля отправлен в Telegram."))
        else:
            return _respond_password_reset_panel(translate("Новый код для сброса пароля отправлен на email."))
    except EmailOtpError as exc:
        reason = str(exc)
        if reason == "not_found":
            return _respond_password_reset_panel(
                translate("Запрос на сброс пароля не найден. Начните заново."),
                category="error",
                status_code=400,
            )
        elif reason == "cooldown":
            return _respond_password_reset_panel(
                translate("Код уже отправлен. Подождите немного перед повторной отправкой."),
                category="error",
                status_code=400,
            )
        elif reason == "telegram_not_linked":
            return _respond_password_reset_panel(
                translate("Сначала привяжите Telegram, чтобы получать код для сброса пароля."),
                category="error",
                status_code=400,
            )
        current_app.logger.exception("Resend password reset in settings failed")
        return _respond_password_reset_panel(
            translate("Не удалось отправить код подтверждения. Попробуйте ещё раз позже."),
            category="error",
            status_code=500,
        )


@login_required
def account_password_reset_cancel():
    from app.services.security import require_csrf

    require_csrf()
    current_email = (current_user.email or "").strip().lower()
    if current_email:
        delete_pending_password_reset(get_pending_password_reset(current_email))
    session.pop(_telegram_session_key("password_reset"), None)
    return _respond_password_reset_panel(translate("Сброс пароля отменён."), category="info")


@login_required
def account_password_reset_verify():
    from app.services.security import require_csrf

    require_csrf()
    current_email = (current_user.email or "").strip().lower()
    new_password = request.form.get("new_password") or ""
    new_password2 = request.form.get("new_password2") or ""
    if new_password != new_password2:
        return _respond_password_reset_panel(translate("Пароли не совпадают"), category="error", status_code=400)
    if not validate_password_strength(new_password):
        return _respond_password_reset_panel(
            translate("Пароль слишком слабый. Используйте минимум 10 символов и комбинацию букв/цифр/символов."),
            category="error",
            status_code=400,
        )

    otp_code = (request.form.get("otp_code") or "").strip()
    if not otp_code.isdigit() or len(otp_code) != 6:
        if password_reset_uses_telegram(current_email):
            return _respond_password_reset_panel(
                translate("Введите 6-значный код из Telegram."),
                category="error",
                status_code=400,
            )
        else:
            return _respond_password_reset_panel(
                translate("Введите 6-значный код из письма."),
                category="error",
                status_code=400,
            )
    ok, reason, pending = verify_pending_password_reset(current_email, otp_code)
    if not ok or not pending:
        if reason == "expired":
            return _respond_password_reset_panel(
                translate("Срок действия кода истёк. Отправьте новый код."),
                category="error",
                status_code=400,
            )
        elif reason == "too_many_attempts":
            return _respond_password_reset_panel(
                translate("Слишком много неверных попыток. Запросите новый код."),
                category="error",
                status_code=400,
            )
        elif reason == "not_found":
            return _respond_password_reset_panel(
                translate("Запрос на сброс пароля не найден. Начните заново."),
                category="error",
                status_code=400,
            )
        return _respond_password_reset_panel(
            translate("Неверный код подтверждения."),
            category="error",
            status_code=400,
        )

    try:
        current_user.set_password(new_password)
        db.session.commit()
        delete_pending_password_reset(pending)
        session.pop(_telegram_session_key("password_reset"), None)
        return _respond_password_reset_panel(translate("Пароль успешно обновлён."))
    except Exception:
        db.session.rollback()
        return _respond_password_reset_panel(
            translate("Не удалось изменить пароль. Попробуйте позже."),
            category="error",
            status_code=500,
        )


@login_required
def account_delete():
    from app.services.security import require_csrf

    require_csrf()
    ip = client_ip()
    locked, _ = throttle_is_locked("delete_account", f"ip:{ip}")
    if locked:
        return _respond_error(translate("Слишком много попыток. Попробуйте позже."), 429)
    payload = _request_payload()
    password = str(payload.get("password") or "")
    confirm_word = str(payload.get("confirm_word") or payload.get("confirm") or "").strip().upper()
    if confirm_word != "DELETE":
        return _respond_error(translate("Введите DELETE для подтверждения удаления"), 400)
    if not _password_is_valid(current_user, password):
        throttle_register_fail("delete_account", f"ip:{ip}", window_seconds=15 * 60, max_fails=5, lock_seconds=30 * 60)
        return _respond_error(translate("Пароль неверный"), 400)
    uid = current_user.id
    try:
        delete_user_account(uid)
        logout_user()
        return _respond_success(translate("Аккаунт удалён"), redirect_to=localized_url("index"))
    except Exception:
        db.session.rollback()
        return _respond_error(translate("Не удалось удалить аккаунт. Попробуйте позже."), 500)


@login_required
def account_change_email_start():
    from app.services.security import require_csrf

    require_csrf()
    ip = client_ip()
    locked, _ = throttle_is_locked("change_email", f"ip:{ip}")
    if locked:
        flash(translate("Слишком много попыток. Попробуйте позже."), "error")
        return _settings_redirect()

    new_email_raw = request.form.get("new_email")
    current_password = request.form.get("password") or ""

    if not current_user.check_password(current_password):
        flash(translate("Пароль неверный"), "error")
        throttle_register_fail("change_email", f"ip:{ip}", window_seconds=15 * 60, max_fails=6, lock_seconds=15 * 60)
        return _settings_redirect()

    try:
        new_email = _normalize_email_input(new_email_raw)
    except EmailNotValidError:
        flash(translate("Введите корректный email"), "error")
        return _settings_redirect()

    try:
        start_pending_email_change(user=current_user, new_email=new_email, lang=_normalized_lang())
        throttle_reset("change_email", f"ip:{ip}")
        flash(translate("Мы отправили код подтверждения на новый email."), "success")
    except EmailOtpError as exc:
        reason = str(exc)
        if reason == "same_email":
            flash(translate("Укажите другой email"), "error")
        elif reason == "email_exists":
            flash(translate("Email уже зарегистрирован"), "error")
        elif reason == "email_already_pending":
            flash(translate("Этот email уже ожидает подтверждения."), "error")
        else:
            current_app.logger.exception("Start pending email change failed")
            flash(translate("Не удалось отправить код подтверждения. Попробуйте ещё раз позже."), "error")
    return _settings_redirect()


@login_required
def api_account_verify_password():
    from app.services.security import require_csrf

    require_csrf()
    payload = _request_payload()
    password = str(payload.get("password") or "")
    is_valid = _password_is_valid(current_user, password)
    if not is_valid:
        return jsonify({"ok": False, "valid": False, "error": translate("Пароль неверный")}), 400
    return jsonify({"ok": True, "valid": True, "message": translate("Пароль подтверждён")})


@login_required
def api_account_change_password():
    from app.services.security import require_csrf

    require_csrf()
    ip = client_ip()
    locked, _ = throttle_is_locked("change_password", f"ip:{ip}")
    if locked:
        return _respond_error(translate("Слишком много попыток. Попробуйте позже."), 429)

    payload = _request_payload()
    current_pw = str(payload.get("current_password") or "")
    new_pw = str(payload.get("new_password") or "")
    new_pw2 = str(payload.get("new_password2") or "")

    if not _password_is_valid(current_user, current_pw):
        throttle_register_fail("change_password", f"ip:{ip}", window_seconds=15 * 60, max_fails=6, lock_seconds=15 * 60)
        return _respond_error(translate("Текущий пароль неверный"), 400)
    if new_pw != new_pw2:
        return _respond_error(translate("Пароли не совпадают"), 400)
    if not validate_password_strength(new_pw):
        return _respond_error(translate("Пароль слишком слабый. Используйте минимум 10 символов и комбинацию букв/цифр/символов."), 400)

    try:
        current_user.set_password(new_pw)
        db.session.commit()
        throttle_reset("change_password", f"ip:{ip}")
        return _respond_success(translate("Пароль успешно изменён"))
    except Exception:
        db.session.rollback()
        return _respond_error(translate("Не удалось изменить пароль. Попробуйте позже."), 500)


@login_required
def api_account_link_email():
    from app.services.security import require_csrf

    require_csrf()
    ip = client_ip()
    locked, _ = throttle_is_locked("link_email", f"ip:{ip}")
    if locked:
        return _respond_error(translate("Слишком много попыток. Попробуйте позже."), 429)

    payload = _request_payload()
    email_raw = payload.get("email")
    password = str(payload.get("password") or "")

    if not _password_is_valid(current_user, password):
        throttle_register_fail("link_email", f"ip:{ip}", window_seconds=15 * 60, max_fails=6, lock_seconds=15 * 60)
        return _respond_error(translate("Пароль неверный"), 400)

    try:
        email = _normalize_email_input(str(email_raw or ""))
    except EmailNotValidError:
        return _respond_error(translate("Введите корректный email"), 400)

    existing_user = User.query.filter_by(email=email).first()
    if existing_user and existing_user.id != current_user.id:
        return _respond_error(translate("Email уже зарегистрирован"), 400)

    current_email = (current_user.email or "").strip().lower()
    if current_email == email:
        return _respond_success(translate("Email уже привязан"), extra={"email": email})

    try:
        start_pending_email_change(user=current_user, new_email=email, lang=_normalized_lang())
    except EmailOtpError as exc:
        reason = str(exc)
        if reason == "same_email":
            return _respond_error(translate("Укажите другой email"), 400)
        if reason == "email_exists":
            return _respond_error(translate("Email уже зарегистрирован"), 400)
        if reason == "email_already_pending":
            return _respond_error(translate("Этот email уже ожидает подтверждения."), 400)
        current_app.logger.exception("Start pending email link failed")
        return _respond_error(translate("Не удалось отправить код подтверждения. Попробуйте ещё раз позже."), 500)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Start pending email link failed")
        return _respond_error(translate("Не удалось отправить код подтверждения. Попробуйте ещё раз позже."), 500)

    throttle_reset("link_email", f"ip:{ip}")
    return _respond_success(
        translate("Мы отправили код подтверждения на новый email."),
        redirect_to=localized_url("dashboard", tab="settings"),
        extra={"pending_email": email},
    )


@login_required
def api_account_unlink_telegram():
    from app.services.security import require_csrf

    require_csrf()
    telegram_account = TelegramAccount.query.filter_by(user_id=current_user.id).first()
    if not telegram_account:
        return _respond_error(translate("Telegram уже отключён"), 400)

    current_email = (current_user.email or "").strip().lower()
    if not current_email or is_telegram_placeholder_email(current_email):
        return _respond_error(translate("Сначала привяжите email, чтобы не потерять доступ к аккаунту."), 400)

    try:
        db.session.delete(telegram_account)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return _respond_error(translate("Не удалось отключить Telegram. Попробуйте позже."), 500)

    _sync_identity_safe(current_user)
    return _respond_success(translate("Telegram отключён"))


@login_required
def api_terminate_session():
    from app.services.security import require_csrf

    require_csrf()
    try:
        session_id = int(request.form.get("session_id") or request.json.get("session_id")) if request.is_json else int(request.form.get("session_id") or "0")
    except Exception:
        flash(translate("Сессия не найдена."), "error")
        return _settings_redirect()
    ok = revoke_user_web_session(current_user.id, session_id, current_token=current_web_session_token())
    if ok:
        flash(translate("Сессия завершена."), "success")
    else:
        flash(translate("Не удалось завершить выбранную сессию."), "error")
    return _settings_redirect()


@login_required
def api_terminate_sessions_all():
    from app.services.security import require_csrf

    require_csrf()
    revoked = revoke_other_web_sessions(current_user.id, current_token=current_web_session_token())
    if revoked > 0:
        flash(translate("Все остальные сессии завершены."), "success")
    else:
        flash(translate("Других активных сессий не найдено."), "info")
    return _settings_redirect()


@login_required
def account_change_email_resend():
    from app.services.security import require_csrf

    require_csrf()
    try:
        resend_pending_email_change(current_user.id)
        flash(translate("Новый код подтверждения отправлен на новый email."), "success")
    except EmailOtpError as exc:
        reason = str(exc)
        if reason == "not_found":
            flash(translate("Запрос на смену email не найден. Начните заново."), "error")
        elif reason == "cooldown":
            flash(translate("Код уже отправлен. Подождите немного перед повторной отправкой."), "error")
        else:
            current_app.logger.exception("Resend pending email change failed")
            flash(translate("Не удалось отправить код подтверждения. Попробуйте ещё раз позже."), "error")
    return _settings_redirect()


@login_required
def account_change_email_cancel():
    from app.services.security import require_csrf

    require_csrf()
    delete_pending_email_change(get_pending_email_change(current_user.id))
    flash(translate("Можно указать другой email."), "info")
    return _settings_redirect()


@login_required
def account_change_email_verify():
    from app.services.security import require_csrf

    require_csrf()
    otp_code = (request.form.get("otp_code") or "").strip()
    if not otp_code.isdigit() or len(otp_code) != 6:
        flash(translate("Введите 6-значный код из письма."), "error")
        return _settings_redirect()

    ok, reason, pending = verify_pending_email_change(current_user.id, otp_code)
    if not ok or not pending:
        if reason == "expired":
            flash(translate("Срок действия кода истёк. Отправьте новый код."), "error")
        elif reason == "too_many_attempts":
            flash(translate("Слишком много неверных попыток. Запросите новый код."), "error")
        elif reason == "not_found":
            flash(translate("Запрос на смену email не найден. Начните заново."), "error")
        else:
            flash(translate("Неверный код подтверждения."), "error")
        return _settings_redirect()

    if User.query.filter_by(email=pending.new_email).first():
        delete_pending_email_change(pending)
        flash(translate("Email уже зарегистрирован"), "error")
        return _settings_redirect()

    try:
        current_user.email = pending.new_email
        db.session.commit()
        delete_pending_email_change(pending)
    except Exception:
        db.session.rollback()
        flash(translate("Не удалось изменить email. Попробуйте позже."), "error")
        return _settings_redirect()

    try:
        cfg = get_remnawave_config()
        if cfg.base_url and cfg.token:
            remnawave_sync_user_identity(cfg, current_user)
    except Exception:
        current_app.logger.exception("Remnawave sync after email change failed")

    flash(translate("Email успешно изменён"), "success")
    return _settings_redirect()


def register(app) -> None:
    app.add_url_rule("/login", endpoint="login", view_func=login, methods=["GET", "POST"])
    app.add_url_rule("/en/login", endpoint="login_en", view_func=login, methods=["GET", "POST"])
    app.add_url_rule("/auth/telegram/status/<code>", endpoint="telegram_auth_status", view_func=telegram_auth_status, methods=["GET"])
    app.add_url_rule("/account/telegram/link", endpoint="telegram_auth_link", view_func=telegram_auth_link, methods=["GET"])
    app.add_url_rule("/r/<code>", endpoint="referral", view_func=referral, methods=["GET"])
    app.add_url_rule("/en/r/<code>", endpoint="referral_en", view_func=referral, methods=["GET"])
    app.add_url_rule("/register", endpoint="register", view_func=register_user, methods=["GET", "POST"])
    app.add_url_rule("/en/register", endpoint="register_en", view_func=register_user, methods=["GET", "POST"])
    app.add_url_rule("/logout", endpoint="logout", view_func=logout, methods=["POST"])
    app.add_url_rule("/account/change-email", endpoint="account_change_email_start", view_func=account_change_email_start, methods=["POST"])
    app.add_url_rule("/account/change-email/resend", endpoint="account_change_email_resend", view_func=account_change_email_resend, methods=["POST"])
    app.add_url_rule("/account/change-email/cancel", endpoint="account_change_email_cancel", view_func=account_change_email_cancel, methods=["POST"])
    app.add_url_rule("/account/change-email/verify", endpoint="account_change_email_verify", view_func=account_change_email_verify, methods=["POST"])
    app.add_url_rule("/account/change-password", endpoint="account_change_password", view_func=account_change_password, methods=["POST"])
    app.add_url_rule("/account/password-reset", endpoint="account_password_reset_start", view_func=account_password_reset_start, methods=["POST"])
    app.add_url_rule("/account/password-reset/resend", endpoint="account_password_reset_resend", view_func=account_password_reset_resend, methods=["POST"])
    app.add_url_rule("/account/password-reset/cancel", endpoint="account_password_reset_cancel", view_func=account_password_reset_cancel, methods=["POST"])
    app.add_url_rule("/account/password-reset/verify", endpoint="account_password_reset_verify", view_func=account_password_reset_verify, methods=["POST"])
    app.add_url_rule("/account/delete", endpoint="account_delete", view_func=account_delete, methods=["POST"])
    app.add_url_rule("/api/account/verify-password", endpoint="api_account_verify_password", view_func=api_account_verify_password, methods=["POST"])
    app.add_url_rule("/api/account/change-password", endpoint="api_account_change_password", view_func=api_account_change_password, methods=["POST"])
    app.add_url_rule("/api/account/link-email", endpoint="api_account_link_email", view_func=api_account_link_email, methods=["POST"])
    app.add_url_rule("/api/account/unlink-telegram", endpoint="api_account_unlink_telegram", view_func=api_account_unlink_telegram, methods=["POST"])
    app.add_url_rule("/api/account/delete", endpoint="api_account_delete", view_func=account_delete, methods=["POST"])
    app.add_url_rule("/account/sessions/terminate", endpoint="api_terminate_session", view_func=api_terminate_session, methods=["POST"])
    app.add_url_rule("/account/sessions/terminate-all", endpoint="api_terminate_sessions_all", view_func=api_terminate_sessions_all, methods=["POST"])
