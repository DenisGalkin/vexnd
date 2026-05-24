from __future__ import annotations

import base64
import io
import math
import os
from datetime import datetime, timedelta

import qrcode
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app.core.extensions import db
from app.domain.models import ReferralSignup, Subscription, User
from app.domain.plans import format_usd_amount, plan_details
from app.services.coupons import coupon_pricing, normalize_coupon_code
from app.services.referrals import get_or_create_referral_code, mask_email
from app.services.security import require_csrf, rotate_csrf_token
from app.services.subscriptions import ensure_remnawave_subscription_url
from app.http.helpers import public_url, translate


@login_required
def dashboard():
    subscription = Subscription.query.filter_by(user_id=current_user.id).first()
    subscription_url = subscription.subscription_url if subscription else None
    now = datetime.utcnow()
    remaining_days = None
    if subscription and subscription.expiry_date:
        try:
            delta = (subscription.expiry_date - now).total_seconds()
            remaining_days = int(math.ceil(delta / 86400.0)) if delta > 0 else 0
        except Exception:
            remaining_days = None
    if subscription and subscription.is_active and subscription.expiry_date and subscription.expiry_date > datetime.utcnow() and not subscription_url:
        subscription_url = ensure_remnawave_subscription_url(current_user, subscription) or None
    qr_code = None
    if subscription_url:
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(subscription_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        qr_code = base64.b64encode(buffered.getvalue()).decode()
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
    return render_template(
        "dashboard.html",
        subscription=subscription,
        subscription_url=subscription_url,
        qr_code=qr_code,
        referral_code=referral_code,
        referral_link=referral_link,
        referrals_total=referrals_total,
        referrals_paid=referrals_paid,
        referrals_list=referrals_list,
        now=now,
        remaining_days=remaining_days,
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
    )


def register(app) -> None:
    app.add_url_rule("/dashboard", endpoint="dashboard", view_func=dashboard, methods=["GET"])
    app.add_url_rule("/en/dashboard", endpoint="dashboard_en", view_func=dashboard, methods=["GET"])
    app.add_url_rule("/account/trial", endpoint="activate_trial", view_func=activate_trial, methods=["POST"])
    app.add_url_rule("/checkout", endpoint="checkout", view_func=checkout, methods=["GET"])
    app.add_url_rule("/en/checkout", endpoint="checkout_en", view_func=checkout, methods=["GET"])
