from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps

from flask import abort, request
from flask_login import current_user
from sqlalchemy import and_, case, func, or_

from app.bot.models import BotTrackedLink, BotTrackedLinkPayment, BotUserState, TelegramAccount
from app.core.config import HTTP
from app.core.extensions import db
from app.domain.models import (
    AdminAuditLog,
    PaymentIntent,
    PaymentIntentPricing,
    PromoActivation,
    PromoCode,
    ReferralCode,
    ReferralSignup,
    Subscription,
    TrialGrant,
    User,
    UserBalance,
    UserCouponRedemption,
    WebSession,
)
from app.domain.plans import format_usd_amount, plan_duration_label
from app.services.bot_admin_links import bot_admin_ids, bot_admin_usernames
from app.services.coupons import intent_pricing
from app.services.promo_codes import decimal_value, get_db_promo, normalize_promo_code, parse_plan_months_csv, promo_conversion_count
from app.services.remnawave import get_remnawave_config, parse_rw_datetime, remnawave_create_user, remnawave_delete_user, remnawave_find_user
from app.services.security import client_ip
from app.services.subscriptions import extend_remnawave_subscription_days


ACTIVE_WINDOW_MINUTES = 15
USER_PAGE_SIZE = 25
PAYMENT_PAGE_SIZE = 30


def _split_env_list(raw: str | None) -> list[str]:
    value = (raw or "").replace("\n", ",")
    return [item.strip() for item in value.split(",") if item.strip()]


def admin_emails() -> set[str]:
    result: set[str] = set()
    for env_name in ("ADMIN_EMAILS", "WEB_ADMIN_EMAILS"):
        for item in _split_env_list(os.environ.get(env_name)):
            result.add(item.strip().lower())
    return result


def admin_user_ids() -> set[int]:
    result: set[int] = set()
    for item in _split_env_list(os.environ.get("ADMIN_USER_IDS")):
        try:
            result.add(int(item))
        except ValueError:
            continue
    return result


def is_admin_user(user: User | None) -> bool:
    if not user or not getattr(user, "id", None):
        return False
    if int(user.id) in admin_user_ids():
        return True
    email = (user.email or "").strip().lower()
    if email and email in admin_emails():
        return True
    account = TelegramAccount.query.filter_by(user_id=user.id).first()
    if not account:
        return False
    if account.telegram_id in bot_admin_ids():
        return True
    normalized_username = (account.username or "").strip().lstrip("@").lower()
    return bool(normalized_username) and normalized_username in bot_admin_usernames()


def admin_required(view_func):
    @wraps(view_func)
    def _wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        if not is_admin_user(current_user):
            abort(403)
        return view_func(*args, **kwargs)

    return _wrapped


def log_admin_action(action: str, target_type: str, target_id: str | int | None = None, summary: str | None = None) -> None:
    db.session.add(
        AdminAuditLog(
            actor_user_id=current_user.id if current_user.is_authenticated else None,
            action=(action or "")[:64],
            target_type=(target_type or "")[:32],
            target_id=(str(target_id)[:64] if target_id is not None else None),
            summary=(summary or "")[:255] or None,
            ip_address=(client_ip() or "")[:64],
            created_at=datetime.utcnow(),
        )
    )


def payment_amount_decimal(intent: PaymentIntent | None) -> Decimal:
    if not intent:
        return Decimal("0.00")
    if getattr(intent, "paid_amount_usd", None):
        return decimal_value(intent.paid_amount_usd)
    if getattr(intent, "expected_amount_usd", None):
        return decimal_value(intent.expected_amount_usd)
    pricing = intent_pricing(intent)
    return decimal_value(pricing.get("final_price"))


def format_money(value: Decimal | float | int | str) -> str:
    return f"${format_usd_amount(value)}"


def user_subscription_status(user: User, subscription: Subscription | None = None, trial: TrialGrant | None = None) -> str:
    if user.is_banned:
        return "banned"
    now = datetime.utcnow()
    subscription = subscription or Subscription.query.filter_by(user_id=user.id).first()
    trial = trial or TrialGrant.query.filter_by(user_id=user.id).first()
    if subscription and subscription.is_active and subscription.expiry_date and subscription.expiry_date > now:
        if trial and trial.expires_at and trial.expires_at >= now and user_successful_payment_count(user.id) == 0:
            return "trial"
        return "active"
    if trial and trial.expires_at and trial.expires_at >= now:
        return "trial"
    if subscription and subscription.expiry_date and subscription.expiry_date <= now:
        return "expired"
    return "expired"


def user_successful_payment_count(user_id: int) -> int:
    return int(
        db.session.query(func.count(PaymentIntent.id))
        .filter(PaymentIntent.user_id == int(user_id), PaymentIntent.plan_months > 0, PaymentIntent.status == "success")
        .scalar()
        or 0
    )


def current_plan_name(subscription: Subscription | None, latest_payment: PaymentIntent | None = None) -> str:
    if subscription and subscription.source == "trial":
        return "Trial"
    months = None
    if subscription and subscription.current_plan_months:
        months = int(subscription.current_plan_months)
    elif latest_payment and latest_payment.plan_months > 0:
        months = int(latest_payment.plan_months)
    return plan_duration_label(months, "ru") if months else "—"


def _active_user_ids_now(now: datetime) -> set[int]:
    threshold = now - timedelta(minutes=ACTIVE_WINDOW_MINUTES)
    ids: set[int] = set()
    rows = (
        db.session.query(WebSession.user_id)
        .filter(WebSession.revoked_at.is_(None), WebSession.last_seen_at >= threshold)
        .distinct()
        .all()
    )
    ids.update(int(row[0]) for row in rows if row[0])
    bot_rows = (
        db.session.query(TelegramAccount.user_id)
        .join(BotUserState, BotUserState.telegram_id == TelegramAccount.telegram_id)
        .filter(BotUserState.updated_at >= threshold)
        .distinct()
        .all()
    )
    ids.update(int(row[0]) for row in bot_rows if row[0])
    return ids


def _successful_payments_query():
    return PaymentIntent.query.filter(PaymentIntent.status == "success")


def dashboard_metrics() -> dict[str, object]:
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    month_start = datetime(now.year, now.month, 1)
    week_start = today_start - timedelta(days=today_start.weekday())
    active_user_ids = _active_user_ids_now(now)

    successful_payments = _successful_payments_query().all()
    external_successful_payments = [item for item in successful_payments if (item.provider or "").strip().lower() != "balance"]
    successful_subscription_payments = [item for item in successful_payments if int(item.plan_months or 0) > 0]
    paying_user_ids = {int(item.user_id) for item in successful_subscription_payments}

    revenue_today = sum(payment_amount_decimal(item) for item in external_successful_payments if (item.paid_at or item.processed_at or item.created_at) >= today_start)
    revenue_month = sum(payment_amount_decimal(item) for item in external_successful_payments if (item.paid_at or item.processed_at or item.created_at) >= month_start)
    avg_check = (sum(payment_amount_decimal(item) for item in external_successful_payments) / Decimal(len(external_successful_payments))).quantize(Decimal("0.01")) if external_successful_payments else Decimal("0.00")
    ltv = (sum(payment_amount_decimal(item) for item in external_successful_payments if int(item.plan_months or 0) > 0) / Decimal(len(paying_user_ids))).quantize(Decimal("0.01")) if paying_user_ids else Decimal("0.00")
    total_users = int(db.session.query(func.count(User.id)).scalar() or 0)
    arpu = (sum(payment_amount_decimal(item) for item in external_successful_payments) / Decimal(total_users)).quantize(Decimal("0.01")) if total_users else Decimal("0.00")

    latest_by_user: dict[int, PaymentIntent] = {}
    for item in successful_subscription_payments:
        existing = latest_by_user.get(int(item.user_id))
        item_dt = item.paid_at or item.processed_at or item.created_at
        existing_dt = (existing.paid_at or existing.processed_at or existing.created_at) if existing else None
        if existing is None or (existing_dt and item_dt and item_dt > existing_dt):
            latest_by_user[int(item.user_id)] = item
    active_subscriptions = Subscription.query.filter(
        Subscription.is_active.is_(True),
        Subscription.expiry_date.isnot(None),
        Subscription.expiry_date > now,
    ).all()
    mrr = Decimal("0.00")
    for subscription in active_subscriptions:
        latest = latest_by_user.get(int(subscription.user_id))
        months = int(subscription.current_plan_months or (latest.plan_months if latest else 0) or 0)
        if latest and months > 0:
            mrr += payment_amount_decimal(latest) / Decimal(months)

    trials_total = int(
        db.session.query(func.count(TrialGrant.id)).filter(TrialGrant.expires_at >= now).scalar() or 0
    )
    trial_users = TrialGrant.query.all()
    converted_trials = 0
    for trial in trial_users:
        paid = (
            db.session.query(PaymentIntent.id)
            .filter(
                PaymentIntent.user_id == trial.user_id,
                PaymentIntent.plan_months > 0,
                PaymentIntent.status == "success",
                PaymentIntent.processed_at.isnot(None),
                PaymentIntent.processed_at >= trial.activated_at,
            )
            .first()
            is not None
        )
        if paid:
            converted_trials += 1
    trial_conversion = round((converted_trials / len(trial_users)) * 100, 1) if trial_users else 0.0

    registrations_day = int(db.session.query(func.count(User.id)).filter(User.created_at >= today_start).scalar() or 0)
    registrations_week = int(db.session.query(func.count(User.id)).filter(User.created_at >= week_start).scalar() or 0)
    registrations_month = int(db.session.query(func.count(User.id)).filter(User.created_at >= month_start).scalar() or 0)

    daily_revenue: list[dict[str, object]] = []
    for offset in range(13, -1, -1):
        day_start = today_start - timedelta(days=offset)
        day_end = day_start + timedelta(days=1)
        amount = sum(payment_amount_decimal(item) for item in external_successful_payments if day_start <= (item.paid_at or item.processed_at or item.created_at) < day_end)
        daily_revenue.append({"label": day_start.strftime("%d.%m"), "amount": amount})

    growth_start = today_start - timedelta(days=13)
    running_total_users = int(
        db.session.query(func.count(User.id))
        .filter(User.created_at < growth_start)
        .scalar()
        or 0
    )
    daily_user_growth: list[dict[str, object]] = []
    daily_registrations: list[dict[str, object]] = []
    for offset in range(13, -1, -1):
        day_start = today_start - timedelta(days=offset)
        day_end = day_start + timedelta(days=1)
        registrations_count = int(
            db.session.query(func.count(User.id))
            .filter(User.created_at >= day_start, User.created_at < day_end)
            .scalar()
            or 0
        )
        running_total_users += registrations_count
        label = day_start.strftime("%d.%m")
        daily_registrations.append({"label": label, "count": registrations_count})
        daily_user_growth.append({"label": label, "count": running_total_users})

    plan_revenue_rows = defaultdict(Decimal)
    for item in successful_subscription_payments:
        plan_revenue_rows[int(item.plan_months or 0)] += payment_amount_decimal(item)
    revenue_by_plan = [
        {"label": plan_duration_label(months, "ru"), "amount": amount}
        for months, amount in sorted(plan_revenue_rows.items(), key=lambda entry: entry[0])
        if months > 0
    ]

    repeat_purchases = int(
        db.session.query(func.count())
        .select_from(
            db.session.query(PaymentIntent.user_id)
            .filter(PaymentIntent.plan_months > 0, PaymentIntent.status == "success")
            .group_by(PaymentIntent.user_id)
            .having(func.count(PaymentIntent.id) >= 2)
            .subquery()
        )
        .scalar()
        or 0
    )
    failed_payments = int(db.session.query(func.count(PaymentIntent.id)).filter(PaymentIntent.status == "failed").scalar() or 0)

    best_channels: list[dict[str, object]] = []
    tracked_rows = (
        db.session.query(
            BotTrackedLink.name,
            func.count(BotTrackedLinkPayment.id).label("payments"),
            func.coalesce(func.sum(BotTrackedLinkPayment.payment_amount_cents), 0).label("amount_cents"),
        )
        .join(BotTrackedLink, BotTrackedLink.id == BotTrackedLinkPayment.link_id)
        .group_by(BotTrackedLink.id, BotTrackedLink.name)
        .order_by(func.coalesce(func.sum(BotTrackedLinkPayment.payment_amount_cents), 0).desc())
        .limit(5)
        .all()
    )
    for row in tracked_rows:
        best_channels.append(
            {
                "name": f"Bot: {row.name}",
                "payments": int(row.payments or 0),
                "revenue": Decimal(int(row.amount_cents or 0)) / Decimal("100"),
            }
        )
    referral_revenue = Decimal("0.00")
    referral_payments = 0
    referred_ids = [row[0] for row in db.session.query(ReferralSignup.referred_user_id).all()]
    if referred_ids:
        for item in successful_subscription_payments:
            if item.user_id in referred_ids:
                referral_revenue += payment_amount_decimal(item)
                referral_payments += 1
        best_channels.append({"name": "Referrals", "payments": referral_payments, "revenue": referral_revenue})
    best_channels = sorted(best_channels, key=lambda item: item["revenue"], reverse=True)[:5]

    return {
        "active_users_now": len(active_user_ids),
        "total_users": total_users,
        "registrations_day": registrations_day,
        "registrations_week": registrations_week,
        "registrations_month": registrations_month,
        "active_subscriptions": len(active_subscriptions),
        "revenue_today": revenue_today,
        "revenue_month": revenue_month,
        "mrr": mrr.quantize(Decimal("0.01")),
        "arpu": arpu,
        "ltv": ltv,
        "trials_total": trials_total,
        "trial_conversion": trial_conversion,
        "daily_revenue": daily_revenue,
        "daily_registrations": daily_registrations,
        "daily_user_growth": daily_user_growth,
        "revenue_by_plan": revenue_by_plan,
        "repeat_purchases": repeat_purchases,
        "failed_payments": failed_payments,
        "avg_check": avg_check,
        "best_channels": best_channels,
    }


def promo_dashboard_metrics() -> dict[str, object]:
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    daily_activations: list[dict[str, object]] = []
    for offset in range(13, -1, -1):
        day_start = today_start - timedelta(days=offset)
        day_end = day_start + timedelta(days=1)
        activations_count = int(
            db.session.query(func.count(PromoActivation.id))
            .filter(PromoActivation.created_at >= day_start, PromoActivation.created_at < day_end)
            .scalar()
            or 0
        )
        daily_activations.append({"label": day_start.strftime("%d.%m"), "count": activations_count})

    promo_rows = promo_list()
    top_codes = sorted(
        promo_rows,
        key=lambda item: (
            decimal_value(item["revenue"]),
            int(item["total_activations"] or 0),
        ),
        reverse=True,
    )[:5]

    total_activations = sum(int(item["total_activations"] or 0) for item in promo_rows)
    paid_activations = sum(int(item["paid_activations"] or 0) for item in promo_rows)
    discount_values = [
        float(decimal_value(item.percent_off))
        for item in PromoCode.query.filter(PromoCode.percent_off.isnot(None)).all()
        if item.percent_off is not None
    ]
    avg_discount = round(sum(discount_values) / len(discount_values), 1) if discount_values else 0.0
    redemption_rate = round((paid_activations / total_activations) * 100, 1) if total_activations else 0.0

    return {
        "daily_activations": daily_activations,
        "top_codes": top_codes,
        "avg_discount": avg_discount,
        "redemption_rate": redemption_rate,
    }


def list_users(*, search: str = "", filter_key: str = "all", min_purchases: int | None = None, page: int = 1) -> dict[str, object]:
    now = datetime.utcnow()
    page = max(1, int(page or 1))
    query = (
        db.session.query(User, Subscription, TelegramAccount, TrialGrant)
        .outerjoin(Subscription, Subscription.user_id == User.id)
        .outerjoin(TelegramAccount, TelegramAccount.user_id == User.id)
        .outerjoin(TrialGrant, TrialGrant.user_id == User.id)
    )
    if search:
        search_text = f"%{search.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(func.coalesce(User.email, "")).like(search_text),
                func.lower(func.coalesce(TelegramAccount.username, "")).like(search_text),
                func.cast(User.id, db.String).like(f"%{search.strip()}%"),
                func.cast(func.coalesce(TelegramAccount.telegram_id, 0), db.String).like(f"%{search.strip()}%"),
            )
        )
    if filter_key == "active":
        query = query.filter(Subscription.is_active.is_(True), Subscription.expiry_date > now, User.is_banned.is_(False))
    elif filter_key == "without_subscription":
        query = query.filter(or_(Subscription.id.is_(None), Subscription.expiry_date.is_(None), Subscription.expiry_date <= now))
    elif filter_key == "expires_today":
        tomorrow = datetime(now.year, now.month, now.day) + timedelta(days=1)
        query = query.filter(Subscription.expiry_date >= datetime(now.year, now.month, now.day), Subscription.expiry_date < tomorrow)
    elif filter_key == "expires_tomorrow":
        tomorrow = datetime(now.year, now.month, now.day) + timedelta(days=1)
        day_after = tomorrow + timedelta(days=1)
        query = query.filter(Subscription.expiry_date >= tomorrow, Subscription.expiry_date < day_after)
    elif filter_key == "expires_3d":
        query = query.filter(Subscription.expiry_date >= now, Subscription.expiry_date < now + timedelta(days=3))
    elif filter_key == "trial_unpaid":
        subq = (
            db.session.query(PaymentIntent.user_id)
            .filter(PaymentIntent.plan_months > 0, PaymentIntent.status == "success")
            .subquery()
        )
        query = query.filter(TrialGrant.id.isnot(None), ~User.id.in_(subq))
    elif filter_key == "banned":
        query = query.filter(User.is_banned.is_(True))

    rows = query.order_by(User.created_at.desc(), User.id.desc()).all()
    result_rows = []
    purchase_counts = {
        user_id: count
        for user_id, count in (
            db.session.query(PaymentIntent.user_id, func.count(PaymentIntent.id))
            .filter(PaymentIntent.plan_months > 0, PaymentIntent.status == "success")
            .group_by(PaymentIntent.user_id)
            .all()
        )
    }
    for user, subscription, telegram, trial in rows:
        purchases = int(purchase_counts.get(user.id, 0) or 0)
        if min_purchases is not None and purchases < int(min_purchases):
            continue
        result_rows.append(
            {
                "id": user.id,
                "email": user.email or "—",
                "telegram_id": telegram.telegram_id if telegram else None,
                "username": telegram.username if telegram else None,
                "created_at": user.created_at,
                "status": user_subscription_status(user, subscription, trial),
                "subscription_expires_at": subscription.expiry_date if subscription else None,
                "is_banned": bool(user.is_banned),
                "purchase_count": purchases,
                "plan_name": current_plan_name(subscription),
            }
        )
    total = len(result_rows)
    start = (page - 1) * USER_PAGE_SIZE
    end = start + USER_PAGE_SIZE
    return {
        "items": result_rows[start:end],
        "page": page,
        "pages": max(1, (total + USER_PAGE_SIZE - 1) // USER_PAGE_SIZE),
        "total": total,
    }


def _find_remote_value(obj, names: set[str]):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in names:
                return value
        for value in obj.values():
            found = _find_remote_value(value, names)
            if found is not None:
                return found
    if isinstance(obj, list):
        for item in obj:
            found = _find_remote_value(item, names)
            if found is not None:
                return found
    return None


def _coerce_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def _user_remote_snapshot(user: User) -> dict[str, object]:
    snapshot = {"used_bytes": None, "limit_bytes": None, "connections": None, "devices": []}
    cfg = get_remnawave_config()
    if not (cfg.base_url and cfg.token):
        return snapshot
    try:
        remote = remnawave_find_user(cfg, user) or {}
    except Exception:
        return snapshot
    snapshot["used_bytes"] = _coerce_int(_find_remote_value(remote, {"usedtrafficbytes", "usedbytes", "trafficusedbytes", "uploadbytes", "downloadbytes"}))
    snapshot["limit_bytes"] = _coerce_int(_find_remote_value(remote, {"trafficlimitbytes", "limitbytes", "totallimitbytes"}))
    connections = _coerce_int(_find_remote_value(remote, {"connectionscount", "activeconnections", "onlineconnections", "devicecount", "devicescount"}))
    snapshot["connections"] = connections
    raw_devices = _find_remote_value(remote, {"devices", "clients", "connections"})
    if isinstance(raw_devices, list):
        parsed_devices = []
        for item in raw_devices[:10]:
            if isinstance(item, dict):
                title = item.get("name") or item.get("deviceName") or item.get("ip") or item.get("id")
                if title:
                    parsed_devices.append(str(title))
            elif item:
                parsed_devices.append(str(item))
        snapshot["devices"] = parsed_devices
    return snapshot


def user_details(user_id: int) -> dict[str, object] | None:
    user = db.session.get(User, int(user_id))
    if not user:
        return None
    telegram = TelegramAccount.query.filter_by(user_id=user.id).first()
    subscription = Subscription.query.filter_by(user_id=user.id).first()
    trial = TrialGrant.query.filter_by(user_id=user.id).first()
    latest_payment = (
        PaymentIntent.query.filter(PaymentIntent.user_id == user.id, PaymentIntent.status == "success", PaymentIntent.plan_months > 0)
        .order_by(PaymentIntent.processed_at.desc(), PaymentIntent.id.desc())
        .first()
    )
    remote = _user_remote_snapshot(user)
    payments = PaymentIntent.query.filter_by(user_id=user.id).order_by(PaymentIntent.created_at.desc()).limit(20).all()
    promo_activations = (
        db.session.query(PromoActivation, PromoCode)
        .join(PromoCode, PromoCode.id == PromoActivation.promo_id)
        .filter(PromoActivation.user_id == user.id)
        .order_by(PromoActivation.created_at.desc())
        .limit(20)
        .all()
    )
    legacy_redemptions = UserCouponRedemption.query.filter_by(user_id=user.id).order_by(UserCouponRedemption.created_at.desc()).limit(20).all()
    balance = db.session.get(UserBalance, user.id)
    referral_code = ReferralCode.query.filter_by(user_id=user.id).first()
    invited_by = ReferralSignup.query.filter_by(referred_user_id=user.id).first()
    referrals = ReferralSignup.query.filter_by(referrer_user_id=user.id).order_by(ReferralSignup.created_at.desc()).limit(20).all()
    sessions = (
        WebSession.query.filter_by(user_id=user.id)
        .order_by(WebSession.last_seen_at.desc())
        .limit(10)
        .all()
    )

    return {
        "user": user,
        "telegram": telegram,
        "subscription": subscription,
        "trial": trial,
        "status": user_subscription_status(user, subscription, trial),
        "plan_name": current_plan_name(subscription, latest_payment),
        "remote": remote,
        "payments": payments,
        "promo_activations": promo_activations,
        "legacy_redemptions": legacy_redemptions,
        "balance_cents": int(balance.amount_cents or 0) if balance else 0,
        "referral_code": referral_code.code if referral_code else None,
        "invited_by": invited_by,
        "referrals": referrals,
        "sessions": sessions,
    }


def create_or_update_promo(form: dict[str, str]) -> PromoCode:
    promo_id = (form.get("promo_id") or "").strip()
    promo = db.session.get(PromoCode, int(promo_id)) if promo_id.isdigit() else None
    if not promo:
        promo = PromoCode(created_at=datetime.utcnow())
        db.session.add(promo)
    promo.code = normalize_promo_code(form.get("code"))
    promo.name = (form.get("name") or "").strip()[:120] or None
    promo.description = (form.get("description") or "").strip()[:255] or None
    promo.is_active = form.get("is_active") == "1"
    promo.percent_off = decimal_text(form.get("percent_off")) if (form.get("percent_off") or "").strip() else None
    promo.fixed_amount_usd = decimal_text(form.get("fixed_amount_usd")) if (form.get("fixed_amount_usd") or "").strip() else None
    promo.bonus_balance_cents = int(round(float((form.get("bonus_balance_usd") or "0").replace(",", ".")) * 100)) if (form.get("bonus_balance_usd") or "").strip() else None
    promo.bonus_days = int(form.get("bonus_days") or 0) if (form.get("bonus_days") or "").strip() else None
    promo.max_activations = int(form.get("max_activations") or 0) if (form.get("max_activations") or "").strip() else None
    promo.max_activations_per_user = int(form.get("max_activations_per_user") or 1) if (form.get("max_activations_per_user") or "").strip() else 1
    promo.audience = (form.get("audience") or "all").strip()[:16]
    promo.plan_months_csv = ",".join(item.strip() for item in (form.get("plan_months_csv") or "").split(",") if item.strip()) or None
    promo.valid_from = datetime.fromisoformat(form["valid_from"]) if (form.get("valid_from") or "").strip() else None
    promo.valid_until = datetime.fromisoformat(form["valid_until"]) if (form.get("valid_until") or "").strip() else None
    if promo.valid_from and promo.valid_until and promo.valid_until < promo.valid_from:
        raise ValueError("promo_date_range_invalid")
    promo.created_by_user_id = current_user.id if current_user.is_authenticated else None
    promo.updated_at = datetime.utcnow()
    return promo


def promo_list() -> list[dict[str, object]]:
    promos = PromoCode.query.order_by(PromoCode.created_at.desc(), PromoCode.id.desc()).all()
    result = []
    for promo in promos:
        activations = PromoActivation.query.filter_by(promo_id=promo.id).all()
        paid_count = promo_conversion_count(promo)
        revenue = Decimal("0.00")
        for activation in activations:
            if activation.payment_intent_token:
                intent = PaymentIntent.query.filter_by(token=activation.payment_intent_token).first()
                if intent and intent.status == "success":
                    revenue += payment_amount_decimal(intent)
        total_activations = int(db.session.query(func.count(PromoActivation.id)).filter(PromoActivation.promo_id == promo.id).scalar() or 0)
        result.append(
            {
                "promo": promo,
                "total_activations": total_activations,
                "paid_activations": paid_count,
                "revenue": revenue,
                "plan_set": parse_plan_months_csv(promo.plan_months_csv),
            }
        )
    return result


def promo_detail(promo_id: int) -> dict[str, object] | None:
    promo = db.session.get(PromoCode, int(promo_id))
    if not promo:
        return None
    activations = (
        db.session.query(PromoActivation, User, TelegramAccount, PaymentIntent)
        .join(User, User.id == PromoActivation.user_id)
        .outerjoin(TelegramAccount, TelegramAccount.user_id == User.id)
        .outerjoin(PaymentIntent, PaymentIntent.token == PromoActivation.payment_intent_token)
        .filter(PromoActivation.promo_id == promo.id)
        .order_by(PromoActivation.created_at.desc())
        .limit(50)
        .all()
    )
    return {"promo": promo, "activations": activations}


def list_payments(*, search: str = "", status: str = "all", page: int = 1) -> dict[str, object]:
    page = max(1, int(page or 1))
    query = (
        db.session.query(PaymentIntent, User, TelegramAccount)
        .join(User, User.id == PaymentIntent.user_id)
        .outerjoin(TelegramAccount, TelegramAccount.user_id == User.id)
    )
    if search:
        search_text = f"%{search.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(func.coalesce(PaymentIntent.external_id, "")).like(search_text),
                func.lower(func.coalesce(PaymentIntent.token, "")).like(search_text),
                func.lower(func.coalesce(User.email, "")).like(search_text),
                func.cast(PaymentIntent.id, db.String).like(f"%{search.strip()}%"),
            )
        )
    if status != "all":
        query = query.filter(PaymentIntent.status == status)
    rows = query.order_by(PaymentIntent.created_at.desc(), PaymentIntent.id.desc()).all()
    items = []
    for intent, user, telegram in rows:
        items.append(
            {
                "intent": intent,
                "user": user,
                "telegram": telegram,
                "amount": payment_amount_decimal(intent),
                "plan_name": plan_duration_label(intent.plan_months, "ru") if int(intent.plan_months or 0) > 0 else "Пополнение баланса",
            }
        )
    total = len(items)
    start = (page - 1) * PAYMENT_PAGE_SIZE
    end = start + PAYMENT_PAGE_SIZE
    return {
        "items": items[start:end],
        "page": page,
        "pages": max(1, (total + PAYMENT_PAGE_SIZE - 1) // PAYMENT_PAGE_SIZE),
        "total": total,
    }


def extend_user_subscription(user: User, days: int) -> None:
    extend_remnawave_subscription_days(user, int(days), source="admin_manual", current_plan_months=None)
    log_admin_action("extend_subscription", "user", user.id, f"days={int(days)}")


def ban_user(user: User, reason: str | None = None) -> None:
    subscription = Subscription.query.filter_by(user_id=user.id).first()
    user.is_banned = True
    user.banned_at = datetime.utcnow()
    user.banned_reason = (reason or "").strip()[:255] or None
    if subscription:
        subscription.is_active = False
        subscription.subscription_url = ""
        subscription.updated_at = datetime.utcnow()
    cfg = get_remnawave_config()
    if cfg.base_url and cfg.token:
        remote = remnawave_find_user(cfg, user)
        remote_uuid = str((remote or {}).get("uuid") or "").strip()
        if remote_uuid:
            try:
                remnawave_delete_user(cfg, remote_uuid)
            except Exception:
                pass
    log_admin_action("ban_user", "user", user.id, user.banned_reason or "manual")


def unban_user(user: User) -> None:
    user.is_banned = False
    user.banned_at = None
    user.banned_reason = None
    subscription = Subscription.query.filter_by(user_id=user.id).first()
    if subscription and subscription.expiry_date and subscription.expiry_date > datetime.utcnow():
        cfg = get_remnawave_config()
        if cfg.base_url and cfg.token:
            try:
                remote = remnawave_create_user(cfg, user, subscription.expiry_date)
                subscription.subscription_url = str((remote or {}).get("subscriptionUrl") or "").strip()
                subscription.is_active = True
                subscription.updated_at = datetime.utcnow()
            except Exception:
                pass
    log_admin_action("unban_user", "user", user.id, "manual")


def send_user_bot_message(user: User, text_value: str) -> None:
    account = TelegramAccount.query.filter_by(user_id=user.id).first()
    if not account:
        raise ValueError("telegram_not_linked")
    bot_token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not bot_token:
        raise RuntimeError("telegram_bot_not_configured")
    resp = HTTP.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={
            "chat_id": int(account.telegram_id),
            "text": text_value[:4000],
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code >= 400 or not payload.get("ok"):
        raise RuntimeError("telegram_send_failed")
    log_admin_action("send_bot_message", "user", user.id, text_value[:120])


def confirm_payment(intent: PaymentIntent) -> None:
    if intent.status == "success" and intent.processed_at:
        return
    user = db.session.get(User, int(intent.user_id))
    if not user:
        raise ValueError("user_not_found")
    from app.services.balance import fulfill_payment_intent
    from app.domain.models import ProcessedPayment

    external_id = (intent.external_id or "").strip() or f"manual-{intent.token}"
    if not ProcessedPayment.query.filter_by(provider=intent.provider, external_id=external_id).first():
        db.session.add(ProcessedPayment(provider=intent.provider, external_id=external_id))
    fulfill_payment_intent(intent, user, external_id)
    intent.external_id = external_id
    intent.status = "success"
    intent.processed_at = intent.processed_at or datetime.utcnow()
    intent.paid_at = intent.paid_at or intent.processed_at
    intent.paid_amount_usd = intent.paid_amount_usd or intent.expected_amount_usd or format_usd_amount(payment_amount_decimal(intent))
    intent.manual_confirmed_at = datetime.utcnow()
    intent.manual_confirmed_by_user_id = current_user.id if current_user.is_authenticated else None
    intent.failure_reason = None
    log_admin_action("confirm_payment", "payment", intent.id, external_id)


def update_payment_status(intent: PaymentIntent, status: str, failure_reason: str | None = None) -> None:
    allowed = {"pending", "failed", "refunded"}
    if status not in allowed:
        raise ValueError("invalid_status")
    if status == "refunded" and intent.status != "success":
        raise ValueError("refund_requires_success")
    intent.status = status
    intent.failure_reason = (failure_reason or "").strip()[:255] or None
    if status == "pending":
        intent.refunded_at = None
    if status == "failed":
        intent.processed_at = None
        intent.paid_at = None
    if status == "refunded":
        intent.refunded_at = datetime.utcnow()
    log_admin_action("payment_status", "payment", intent.id, status)
