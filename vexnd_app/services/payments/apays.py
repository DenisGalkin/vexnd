from __future__ import annotations

import hashlib
import os
import secrets
from decimal import Decimal

from vexnd_app.config import HTTP
from vexnd_app.extensions import db
from vexnd_app.models import PaymentIntent, ProcessedPayment, User
from vexnd_app.plans import plan_price_usd
from vexnd_app.security.webhooks import intent_not_expired, payment_processing_lock
from vexnd_app.services.coupons import apply_coupon_redemption_for_intent
from vexnd_app.services.referrals import apply_referral_bonus_if_eligible
from vexnd_app.services.subscriptions import create_remnawave_subscription


def apays_credentials() -> tuple[str, str]:
    client_id = (os.environ.get("APAYS_CLIENT_ID") or "").strip()
    secret_key = (os.environ.get("APAYS_SECRET_KEY") or "").strip()
    if not client_id or not secret_key:
        raise RuntimeError("APAYS credentials not configured")
    return client_id, secret_key


def plan_price_rub_kop(plan_months: int, *, amount_usd: Decimal | None = None) -> int:
    plan_months = int(plan_months)
    explicit_map = {}
    for months in (1, 6, 12):
        value = (os.environ.get(f"APAYS_PRICE_RUB_{months}") or "").strip()
        if value:
            try:
                explicit_map[months] = float(value)
            except Exception:
                pass
    if plan_months in explicit_map:
        rub = explicit_map[plan_months]
    else:
        rate_str = (os.environ.get("APAYS_USD_TO_RUB") or "100").strip()
        try:
            rate = float(rate_str)
        except Exception:
            rate = 100.0
        rub = float(amount_usd if amount_usd is not None else plan_price_usd(plan_months)) * rate
    return int(round(rub * 100))


def apays_md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def create_apays_order(user_id: int, plan_months: int, *, amount_usd: Decimal | None = None) -> tuple[dict, str]:
    client_id, secret_key = apays_credentials()
    amount_kop = plan_price_rub_kop(plan_months, amount_usd=amount_usd)
    order_id = secrets.token_hex(16)
    sign = apays_md5(f"{order_id}:{amount_kop}:{secret_key}")
    params = {"client_id": client_id, "order_id": order_id, "amount": amount_kop, "sign": sign}
    user_obj = User.query.get(user_id)
    if user_obj and (user_obj.email or "").strip():
        params["email"] = user_obj.email.strip()
    resp = HTTP.get("https://apays.io/backend/create_order", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("status"):
        raise RuntimeError(f"APAYS create_order failed: {data}")
    return data, order_id


def apays_get_order_status(order_id: str) -> str | None:
    client_id, secret_key = apays_credentials()
    sign = apays_md5(f"{order_id}:{secret_key}")
    params = {"client_id": client_id, "order_id": order_id, "sign": sign}
    resp = HTTP.get("https://apays.io/backend/get_order", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("status"):
        return None
    return str(data.get("order_status") or "").strip() or None


def apays_process_approved(order_id: str) -> tuple[bool, str]:
    if not order_id:
        return False, "missing order_id"
    with payment_processing_lock("apays", order_id):
        if ProcessedPayment.query.filter_by(provider="apays", external_id=order_id).first():
            return True, "duplicate"
        intent = PaymentIntent.query.filter_by(provider="apays", external_id=order_id).first()
        if not intent:
            return False, "intent not found"
        if not intent_not_expired(intent, hours=24):
            return False, "intent expired"
        status = apays_get_order_status(order_id)
        if status != "approve":
            return False, f"not approved ({status})"
        user_obj = User.query.get(intent.user_id)
        if not user_obj:
            return False, "user not found"
        create_remnawave_subscription(user_obj, int(intent.plan_months), strict=True)
        apply_coupon_redemption_for_intent(intent)
        apply_referral_bonus_if_eligible(user_obj)
        db.session.add(ProcessedPayment(provider="apays", external_id=order_id))
        intent.processed_at = datetime.utcnow()
        db.session.commit()
        return True, "ok"
