from __future__ import annotations

import base64
import io
import math
import os
from datetime import datetime, timedelta
from typing import Any

import qrcode
from flask import flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app.bot.common import format_bytes
from app.bot.models import TelegramAccount
from app.bot.subscriptions import local_subscription_snapshot, remnawave_subscription_snapshot, snapshot_has_missing_remote_subscription
from app.core.extensions import db
from app.domain.models import BalanceTransaction, PaymentIntent, ReferralSignup, Subscription, User, UserNotificationPreference
from app.domain.plans import format_usd_amount, plan_details, plan_duration_label
from app.services.balance import TOPUP_PRESET_CENTS, can_pay_for_plan_with_balance, format_balance_cents, user_balance_cents
from app.services.coupons import coupon_pricing, intent_pricing, normalize_coupon_code
from app.services.email_change_otp import get_pending_email_change
from app.services.email_otp import OTP_TTL_MINUTES
from app.services.password_reset_otp import get_pending_password_reset
from app.services.payments.reconcile import process_payment_intent
from app.services.referrals import get_or_create_referral_code, mask_email
from app.services.remnawave import is_telegram_placeholder_email
from app.services.security import require_csrf, rotate_csrf_token
from app.services.telegram_auth import CHALLENGE_TTL_MINUTES, create_telegram_auth_challenge, get_active_challenge
from app.services.telegram_links import telegram_bot_deeplink
from app.services.web_sessions import current_web_session_token, user_web_sessions
from app.http.helpers import public_url, translate


def _dashboard_security_context() -> dict[str, object]:
    telegram_account = TelegramAccount.query.filter_by(user_id=current_user.id).first()
    current_email_value = (current_user.email or "").strip().lower()
    pending_password_reset = get_pending_password_reset(current_email_value) if current_email_value else None
    password_reset_delivery_hint = None
    if telegram_account:
        if telegram_account.username:
            password_reset_delivery_hint = f"@{telegram_account.username.lstrip('@')}"
        else:
            password_reset_delivery_hint = f"ID {telegram_account.telegram_id}"
    return {
        "telegram_account": telegram_account,
        "current_email_display": current_user.email if not is_telegram_placeholder_email(current_email_value) else "",
        "current_email_missing": is_telegram_placeholder_email(current_email_value),
        "pending_password_reset": pending_password_reset,
        "pending_password_reset_masked": mask_email(pending_password_reset.email) if pending_password_reset else None,
        "password_reset_delivery_hint": password_reset_delivery_hint,
    }


def _requested_partial_targets() -> list[str]:
    header = request.headers.get("X-Partial-Update", "")
    return [target.strip() for target in header.split(",") if target.strip()]


def _build_dashboard_referrals_context() -> dict[str, object]:
    referral_code = get_or_create_referral_code(current_user)
    referral_link = public_url("referral", code=referral_code, canonical=True)
    referrals_list = []
    try:
        query = (
            db.session.query(ReferralSignup, User.email)
            .outerjoin(User, User.id == ReferralSignup.referred_user_id)
            .filter(ReferralSignup.referrer_user_id == current_user.id)
            .order_by(ReferralSignup.created_at.desc())
            .all()
        )
        for signup, referred_email in query:
            referrals_list.append(
                {
                    "email_masked": mask_email(referred_email) if referred_email else "—",
                    "created_at": signup.created_at,
                    "first_paid_at": signup.first_paid_at,
                    "bonuses_applied_at": signup.bonuses_applied_at,
                }
            )
    except Exception:
        referrals_list = []
    try:
        referrals_total, referrals_paid = (
            db.session.query(
                func.count(ReferralSignup.id),
                func.count(ReferralSignup.bonuses_applied_at),
            )
            .filter(ReferralSignup.referrer_user_id == current_user.id)
            .one()
        )
    except Exception:
        referrals_total = 0
        referrals_paid = 0
    return {
        "referral_code": referral_code,
        "referral_link": referral_link,
        "referrals_total": referrals_total,
        "referrals_paid": referrals_paid,
        "referrals_list": referrals_list,
    }


def _build_dashboard_settings_context() -> dict[str, object]:
    transactions = []
    subscription_plan_name = None
    balance_amount_cents = user_balance_cents(current_user.id)
    try:
        intents = (
            db.session.query(PaymentIntent)
            .filter(PaymentIntent.user_id == current_user.id)
            .order_by(PaymentIntent.created_at.desc())
            .limit(20)
            .all()
        )
        for intent in intents:
            pricing = intent_pricing(intent)
            if subscription_plan_name is None and intent.processed_at:
                subscription_plan_name = plan_duration_label(intent.plan_months)
            transactions.append(
                {
                    "id": intent.id,
                    "date": intent.processed_at or intent.created_at,
                    "amount": f"{format_usd_amount(pricing['final_price'])} USD",
                    "original_amount": format_usd_amount(pricing["original_price"]),
                    "plan_name": plan_duration_label(intent.plan_months) if int(intent.plan_months or 0) > 0 else translate("Пополнение баланса"),
                    "provider": (intent.provider or "").strip() or "—",
                    "coupon_code": pricing.get("coupon_code") or None,
                    "status": "success" if intent.processed_at else "pending",
                    "kind": getattr(intent, "purpose", "subscription"),
                }
            )
    except Exception:
        transactions = []
        subscription_plan_name = None
    try:
        balance_entries = (
            db.session.query(BalanceTransaction)
            .filter(BalanceTransaction.user_id == current_user.id)
            .order_by(BalanceTransaction.created_at.desc())
            .limit(20)
            .all()
        )
    except Exception:
        balance_entries = []

    sessions = []
    try:
        sessions = user_web_sessions(
            current_user.id,
            current_token=current_web_session_token(),
            locale=(current_user.lang or "en"),
        )
    except Exception:
        sessions = []

    security_context = _dashboard_security_context()
    telegram_account = security_context["telegram_account"]
    pending_email_change = get_pending_email_change(current_user.id)
    telegram_auth = None
    if not telegram_account:
        session_key = "tg_auth_code:link"
        challenge = get_active_challenge(request.args.get("tg_link") or "", purpose="link")
        if not challenge or challenge.target_user_id != current_user.id:
            challenge = get_active_challenge(session.get(session_key), purpose="link")
        if challenge and challenge.target_user_id != current_user.id:
            challenge = None
        if not challenge:
            challenge = create_telegram_auth_challenge(purpose="link", target_user_id=current_user.id)
            session[session_key] = challenge.code
        start_value = f"link_{challenge.code}"
        telegram_auth = {
            "code": challenge.code,
            "minutes": CHALLENGE_TTL_MINUTES,
            "bot_url": telegram_bot_deeplink(start_value),
            "status_url": url_for("telegram_auth_status", code=challenge.code),
            "command": f"/start {start_value}",
        }

    notification_preferences = UserNotificationPreference.query.filter_by(user_id=current_user.id).first()

    return {
        "transactions": transactions,
        "sessions": sessions,
        "subscription_plan_name": subscription_plan_name,
        "balance_amount_cents": balance_amount_cents,
        "balance_amount_text": format_balance_cents(balance_amount_cents),
        "balance_entries": balance_entries,
        "topup_presets": list(TOPUP_PRESET_CENTS),
        "telegram_auth": telegram_auth,
        "pending_email_change": pending_email_change,
        "pending_email_change_masked": mask_email(pending_email_change.new_email) if pending_email_change else None,
        "email_change_otp_ttl_minutes": OTP_TTL_MINUTES,
        "notify_expiry": bool(notification_preferences.notify_expiry) if notification_preferences else False,
        "notify_maintenance": bool(notification_preferences.notify_maintenance) if notification_preferences else False,
        "notify_news": bool(notification_preferences.notify_news) if notification_preferences else False,
        **security_context,
    }


def _subscription_qr_code(subscription_url: str | None) -> str | None:
    if not subscription_url:
        return None
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(subscription_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode()


def _build_dashboard_subscription_context(*, force_remote_refresh: bool = False) -> dict[str, Any]:
    now = datetime.utcnow()
    subscription, local_snapshot = local_subscription_snapshot(current_user)
    snapshot = dict(local_snapshot)
    if force_remote_refresh:
        try:
            snapshot = remnawave_subscription_snapshot(current_user, force_refresh=True, schedule_async_refresh=False)
        except Exception:
            snapshot = dict(local_snapshot)
        subscription = Subscription.query.filter_by(user_id=current_user.id).first()

    subscription_missing_remote = snapshot_has_missing_remote_subscription(snapshot)
    if subscription_missing_remote:
        subscription = None

    expiry_date = snapshot.get("expiry_date") or (subscription.expiry_date if subscription else None)
    is_active = bool(snapshot.get("active"))
    if expiry_date is not None:
        is_active = bool(expiry_date > now and (subscription.is_active if subscription else snapshot.get("active", False)))
    has_subscription_record = bool(subscription) and not subscription_missing_remote
    remaining_days = None
    if expiry_date:
        try:
            delta = (expiry_date - now).total_seconds()
            remaining_days = int(math.ceil(delta / 86400.0)) if delta > 0 else 0
        except Exception:
            remaining_days = None

    subscription_url = (snapshot.get("subscription_url") or "").strip()
    if not subscription_url and subscription:
        subscription_url = (subscription.subscription_url or "").strip()
    subscription_url = subscription_url or None

    used_bytes = snapshot.get("used_bytes")
    limit_bytes = snapshot.get("limit_bytes")
    traffic_progress = None
    if isinstance(limit_bytes, int) and limit_bytes > 0 and isinstance(used_bytes, int) and used_bytes >= 0:
        traffic_progress = max(0, min(100, round((used_bytes / limit_bytes) * 100, 1)))
    subscription_sync_pending = bool(
        is_active and (
            not subscription_url
            or used_bytes is None
            or limit_bytes is None
        )
    )

    return {
        "subscription": subscription,
        "now": now,
        "remaining_days": remaining_days,
        "subscription_url": subscription_url,
        "subscription_missing_remote": subscription_missing_remote,
        "used_bytes": used_bytes,
        "limit_bytes": limit_bytes,
        "used_bytes_text": format_bytes(used_bytes),
        "limit_bytes_text": format_bytes(limit_bytes),
        "traffic_progress": traffic_progress,
        "subscription_sync_pending": subscription_sync_pending,
        "qr_code": _subscription_qr_code(subscription_url),
        "has_subscription_record": has_subscription_record,
        "is_active": is_active,
        "balance_can_pay_1m": can_pay_for_plan_with_balance(current_user.id, 1)[0],
        "balance_can_pay_3m": can_pay_for_plan_with_balance(current_user.id, 3)[0],
        "balance_can_pay_12m": can_pay_for_plan_with_balance(current_user.id, 12)[0],
    }
@login_required
def dashboard():
    partial_targets = _requested_partial_targets()
    pending_intents = (
        db.session.query(PaymentIntent)
        .filter(
            PaymentIntent.user_id == current_user.id,
            PaymentIntent.plan_months > 0,
            PaymentIntent.processed_at.is_(None),
        )
        .order_by(PaymentIntent.created_at.desc())
        .limit(3)
        .all()
    )
    for intent in pending_intents:
        try:
            processed, _msg = process_payment_intent(intent)
            if processed:
                break
        except Exception as exc:
            print(f"Dashboard payment reconcile failed for intent {intent.id}: {exc}")
            try:
                db.session.rollback()
            except Exception:
                pass
    if partial_targets:
        fragments: dict[str, str] = {}
        referral_context: dict[str, object] | None = None
        settings_context: dict[str, object] | None = None
        for target in partial_targets:
            if target == "tab-ref":
                if referral_context is None:
                    referral_context = _build_dashboard_referrals_context()
                fragments[target] = render_template("dashboard/_referrals_tab.html", **referral_context)
            elif target == "section-account":
                if settings_context is None:
                    settings_context = _build_dashboard_settings_context()
                fragments[target] = render_template("account/_account_section.html", **settings_context)
            elif target == "section-security":
                if settings_context is None:
                    settings_context = _build_dashboard_settings_context()
                fragments[target] = render_template("account/_security_section.html", **settings_context)
            elif target == "password-reset-panel":
                if settings_context is None:
                    settings_context = _build_dashboard_settings_context()
                fragments[target] = render_template("account/_password_reset_panel.html", **settings_context)
            elif target == "section-devices":
                if settings_context is None:
                    settings_context = _build_dashboard_settings_context()
                fragments[target] = render_template("account/_devices_section.html", sessions=settings_context["sessions"])
        return jsonify(
            {
                "ok": True,
                "fragments": fragments,
            }
        )
    force_remote_refresh = request.args.get("sync_subscription") == "1" or not partial_targets
    subscription_context = _build_dashboard_subscription_context(force_remote_refresh=force_remote_refresh)
    referral_context = _build_dashboard_referrals_context()
    settings_context = _build_dashboard_settings_context()
    return render_template(
        "dashboard.html",
        **referral_context,
        **settings_context,
        **subscription_context,
    )


@login_required
def activate_trial():
    rotate_csrf_token()
    flash(translate("Пробный доступ доступен только в Telegram-боте."), "info")
    return redirect(url_for("dashboard"))


@login_required
def checkout():
    try:
        plan_months = int(request.args.get("plan", "1"))
    except ValueError:
        plan_months = 1
    plan_info = plan_details(plan_months)
    plan_months = plan_info["months"]
    coupon_code = normalize_coupon_code(request.args.get("coupon"))
    pricing = coupon_pricing(plan_months, coupon_code, current_user.id if current_user.is_authenticated else None)
    price = format_usd_amount(pricing["final_price"])
    start_dt = datetime.utcnow()
    end_dt = start_dt + timedelta(days=30 * plan_months)
    payment_methods = [{"slug": "cryptobot", "name": "Crypto Bot", "icon": "🤖", "class": "crypto preferred", "preferred": True}]
    if (os.environ.get("HELEKET_MERCHANT_ID") or "").strip() and (os.environ.get("HELEKET_API_KEY") or "").strip():
        payment_methods.append({"slug": "heleket", "name": "Heleket", "icon": "🪙", "class": "crypto preferred", "preferred": True})
    if (os.environ.get("PLATEGA_MERCHANT_ID") or "").strip() and (os.environ.get("PLATEGA_SECRET") or "").strip():
        payment_methods.append({"slug": "platega", "name": "Platega.io", "icon": "🌍", "class": "ru preferred", "preferred": True})
    if (os.environ.get("CRYSTALPAY_AUTH_LOGIN") or "").strip() and (os.environ.get("CRYSTALPAY_AUTH_SECRET") or "").strip():
        payment_methods.append({"slug": "crystalpay", "name": "Crystal Pay", "icon": "💎", "class": "crypto", "preferred": False})
    balance_ready, _balance_pricing = can_pay_for_plan_with_balance(current_user.id, plan_months, coupon_code)
    if balance_ready:
        payment_methods.insert(0, {"slug": "balance", "name": translate("Внутренний баланс"), "icon": "💰", "class": "preferred", "preferred": True})
    return render_template(
        "checkout.html",
        plan=plan_months,
        price=price,
        original_price=format_usd_amount(pricing["original_price"]),
        discount_amount=format_usd_amount(pricing["discount_amount"]),
        coupon_code=(pricing["coupon_code"] or ""),
        coupon_applied=pricing["coupon_applied"],
        payment_methods=payment_methods,
        plan_features=plan_info["features"],
        start_dt=start_dt,
        end_dt=end_dt,
        user_email=(current_user.email if current_user.is_authenticated else ""),
        balance_amount_text=format_balance_cents(user_balance_cents(current_user.id)),
    )


def register(app) -> None:
    app.add_url_rule("/dashboard", endpoint="dashboard", view_func=dashboard, methods=["GET"])
    app.add_url_rule("/en/dashboard", endpoint="dashboard_en", view_func=dashboard, methods=["GET"])
    app.add_url_rule("/account/trial", endpoint="activate_trial", view_func=activate_trial, methods=["POST"])
    app.add_url_rule("/checkout", endpoint="checkout", view_func=checkout, methods=["GET"])
    app.add_url_rule("/en/checkout", endpoint="checkout_en", view_func=checkout, methods=["GET"])
