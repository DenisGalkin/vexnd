from __future__ import annotations

from datetime import datetime

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.core.extensions import db
from app.domain.models import PaymentIntent, ReferralCode, ReferralFingerprint, ReferralSignup, Subscription, User, UserSecurity
from app.services.referrals import mask_email
from app.services.security import client_ip, device_fingerprint, rotate_csrf_token, throttle_is_locked, throttle_register_fail, throttle_reset, validate_password_strength
from app.http.helpers import get_locale, localized_url, redirect_localized, translate


def login():
    if current_user.is_authenticated:
        return redirect_localized("dashboard")
    if request.method == "POST":
        ip = client_ip()
        email = request.form.get("email")
        password = request.form.get("password")
        remember = True if request.form.get("remember_me") else False
        if email:
            locked, _ = throttle_is_locked("login", f"email:{email.lower().strip()}")
            if locked:
                flash(translate("Слишком много попыток входа. Попробуйте позже."), "error")
                return render_template("auth/login.html"), 429
        locked, _ = throttle_is_locked("login", f"ip:{ip}")
        if locked:
            flash(translate("Слишком много попыток входа. Попробуйте позже."), "error")
            return render_template("auth/login.html"), 429
        user = User.query.filter_by(email=email.lower().strip() if email else email).first()
        if user and user.check_password(password):
            rotate_csrf_token()
            login_user(user, remember=remember)
            chosen = session.get("lang") or get_locale()
            if chosen not in ("ru", "en"):
                chosen = "en"
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
    return render_template("auth/login.html")


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
    if request.method == "POST":
        ip = client_ip()
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
        user = User(email=email, lang=chosen_lang)
        user.set_password(password)
        try:
            db.session.add(user)
            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            if User.query.filter_by(email=email).first():
                flash(translate("Email уже зарегистрирован"), "error")
                return render_template("auth/register.html", referrer_email=referrer_masked)
            raise
        ref_code = (session.get("ref_code") or "").strip().upper()
        try:
            if ref_code and referral_ok:
                rc = ReferralCode.query.filter_by(code=ref_code).first()
                if rc and rc.user_id and rc.user_id != user.id and not ReferralSignup.query.filter_by(referred_user_id=user.id).first():
                    db.session.add(ReferralSignup(referrer_user_id=rc.user_id, referred_user_id=user.id, code_used=ref_code))
                    ip2 = request.headers.get("X-Forwarded-For", request.remote_addr) or request.remote_addr
                    ua2 = request.headers.get("User-Agent", "")[:250]
                    fp2 = device_fingerprint(ip2, ua2)
                    if fp2 and not ReferralFingerprint.query.filter_by(fingerprint=fp2).first():
                        db.session.add(ReferralFingerprint(fingerprint=fp2, referred_user_id=user.id))
                    db.session.commit()
        except Exception:
            current_app.logger.exception("Referral link on register failed")
            try:
                db.session.rollback()
            except Exception:
                pass
        finally:
            session.pop("ref_code", None)
            session.pop("ref_code_set_at", None)
        rotate_csrf_token()
        login_user(user, remember=True)
        session["lang"] = chosen_lang
        flash(translate("Регистрация успешна! Добро пожаловать."), "success")
        throttle_reset("register", f"ip:{ip}")
        return redirect_localized("dashboard")
    return render_template("auth/register.html", referrer_email=referrer_masked)


@login_required
def logout():
    rotate_csrf_token()
    logout_user()
    return redirect(url_for("index"))


@login_required
def account_change_password():
    ip = client_ip()
    locked, _ = throttle_is_locked("change_password", f"ip:{ip}")
    if locked:
        flash(translate("Слишком много попыток. Попробуйте позже."), "error")
        return redirect(url_for("dashboard"))
    current_pw = request.form.get("current_password") or ""
    new_pw = request.form.get("new_password") or ""
    new_pw2 = request.form.get("new_password2") or ""
    if not current_user.check_password(current_pw):
        flash(translate("Текущий пароль неверный"), "error")
        throttle_register_fail("change_password", f"ip:{ip}", window_seconds=15 * 60, max_fails=6, lock_seconds=15 * 60)
        return redirect(url_for("dashboard"))
    if new_pw != new_pw2:
        flash(translate("Пароли не совпадают"), "error")
        return redirect(url_for("dashboard"))
    if not validate_password_strength(new_pw):
        flash(translate("Пароль слишком слабый. Используйте минимум 10 символов и комбинацию букв/цифр/символов."), "error")
        return redirect(url_for("dashboard"))
    try:
        current_user.set_password(new_pw)
        db.session.commit()
        throttle_reset("change_password", f"ip:{ip}")
        flash(translate("Пароль успешно изменён"), "success")
    except Exception:
        db.session.rollback()
        flash(translate("Не удалось изменить пароль. Попробуйте позже."), "error")
    return redirect(url_for("dashboard"))


@login_required
def account_delete():
    ip = client_ip()
    locked, _ = throttle_is_locked("delete_account", f"ip:{ip}")
    if locked:
        flash(translate("Слишком много попыток. Попробуйте позже."), "error")
        return redirect(url_for("dashboard"))
    password = request.form.get("password") or ""
    confirm = (request.form.get("confirm") or "").strip().upper()
    if confirm != "DELETE":
        flash(translate("Введите DELETE для подтверждения удаления"), "error")
        return redirect(url_for("dashboard"))
    if not current_user.check_password(password):
        flash(translate("Пароль неверный"), "error")
        throttle_register_fail("delete_account", f"ip:{ip}", window_seconds=15 * 60, max_fails=5, lock_seconds=30 * 60)
        return redirect(url_for("dashboard"))
    uid = current_user.id
    try:
        Subscription.query.filter_by(user_id=uid).delete()
        PaymentIntent.query.filter_by(user_id=uid).delete()
        user = User.query.get(uid)
        logout_user()
        if user:
            db.session.delete(user)
        db.session.commit()
        flash(translate("Аккаунт удалён"), "success")
        return redirect(url_for("index"))
    except Exception:
        db.session.rollback()
        flash(translate("Не удалось удалить аккаунт. Попробуйте позже."), "error")
        return redirect(url_for("dashboard"))


def register(app) -> None:
    app.add_url_rule("/login", endpoint="login", view_func=login, methods=["GET", "POST"])
    app.add_url_rule("/en/login", endpoint="login_en", view_func=login, methods=["GET", "POST"])
    app.add_url_rule("/r/<code>", endpoint="referral", view_func=referral, methods=["GET"])
    app.add_url_rule("/en/r/<code>", endpoint="referral_en", view_func=referral, methods=["GET"])
    app.add_url_rule("/register", endpoint="register", view_func=register_user, methods=["GET", "POST"])
    app.add_url_rule("/en/register", endpoint="register_en", view_func=register_user, methods=["GET", "POST"])
    app.add_url_rule("/logout", endpoint="logout", view_func=logout, methods=["POST"])
    app.add_url_rule("/account/change-password", endpoint="account_change_password", view_func=account_change_password, methods=["POST"])
    app.add_url_rule("/account/delete", endpoint="account_delete", view_func=account_delete, methods=["POST"])
