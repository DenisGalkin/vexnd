from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime
from decimal import Decimal

from flask_login import current_user

from app.core.config import HTTP
from app.core.extensions import db
from app.domain.models import PaymentIntent, ProcessedPayment, User
from app.domain.plans import format_usd_amount, plan_duration_label, plan_price_usd
from app.http.security.webhooks import intent_not_expired, payment_processing_lock
from app.services.coupons import apply_coupon_redemption_for_intent, intent_expected_amounts
from app.services.payment_intents import mark_processed_payment
from app.services.referrals import apply_referral_bonus_if_eligible
from app.services.security import get_webhook_secret
from app.services.subscriptions import create_remnawave_subscription
from app.http.helpers import public_url


def crystal_credentials() -> tuple[str, str]:
    login = (os.environ.get("CRYSTALPAY_AUTH_LOGIN") or "").strip()
    secret = (os.environ.get("CRYSTALPAY_AUTH_SECRET") or "").strip()
    if not login or not secret:
        raise RuntimeError("Crystal Pay credentials not configured")
    return login, secret


def create_crystal_invoice(user_id: int, plan_months: int, *, amount_usd: Decimal | None = None) -> tuple[dict, str]:
    amount = format_usd_amount(amount_usd if amount_usd is not None else plan_price_usd(plan_months))
    auth_login, auth_secret = crystal_credentials()
    intent_token = secrets.token_urlsafe(24)
    redirect_url = public_url("crystalpay_return", token=intent_token)
    cp_wh_secret = get_webhook_secret("CRYSTALPAY_WEBHOOK_PATH_SECRET")
    if cp_wh_secret:
        callback_url = public_url("crystalpay_webhook_secret", secret=cp_wh_secret)
    else:
        callback_url = public_url("crystalpay_webhook")
    invoice_request = {
        "auth_login": auth_login,
        "auth_secret": auth_secret,
        "amount": amount,
        "currency": "USD",
        "type": "purchase",
        "lifetime": 60,
        "description": f"VEXND subscription for {plan_duration_label(plan_months, 'en')}",
        "payer_details": {"email": current_user.email} if current_user.is_authenticated and current_user.email else None,
        "extra": intent_token,
        "redirect_url": redirect_url,
        "callback_url": callback_url,
    }
    invoice_request = {k: v for k, v in invoice_request.items() if v is not None}
    resp = HTTP.post("https://api.crystalpay.io/v3/invoice/create/", json=invoice_request, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(f"Error creating Crystal Pay invoice: {data.get('errors') or data}")
    return data, intent_token


def crystal_invoice_info(invoice_id: str) -> dict:
    auth_login, auth_secret = crystal_credentials()
    req_body = {"auth_login": auth_login, "auth_secret": auth_secret, "id": invoice_id}
    resp = HTTP.post("https://api.crystalpay.io/v3/invoice/info/", json=req_body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(f"Error fetching Crystal Pay invoice info: {data.get('errors') or data}")
    return data


def crystalpay_invoice_matches_intent(info: dict, intent: PaymentIntent) -> bool:
    if not isinstance(info, dict) or not intent:
        return False
    if str(info.get("extra") or "").strip() != str(intent.token or "").strip():
        return False
    amount = str(info.get("amount") or "").strip()
    if amount:
        try:
            paid_amount = Decimal(amount)
        except Exception:
            return False
        expected_amounts = intent_expected_amounts(intent)
        if expected_amounts and all(abs(paid_amount - value) > Decimal("0.000000001") for value in expected_amounts):
            return False
    currency = str(info.get("currency") or "").strip().upper()
    if currency and currency != "USD":
        return False
    return True


def crystalpay_process_paid_invoice(invoice_id: str, payload_token: str | None = None) -> tuple[bool, str]:
    invoice_id = (invoice_id or "").strip()
    payload_token = (payload_token or "").strip() or None
    if not invoice_id:
        return False, "missing invoice_id"
    with payment_processing_lock("crystalpay", invoice_id):
        if ProcessedPayment.query.filter_by(provider="crystalpay", external_id=invoice_id).first():
            return True, "duplicate"
        info = crystal_invoice_info(invoice_id)
        if str(info.get("state") or "").strip() != "payed":
            return False, "not paid"
        intent = PaymentIntent.query.filter_by(provider="crystalpay", external_id=invoice_id).first()
        if not intent and payload_token:
            intent = PaymentIntent.query.filter_by(provider="crystalpay", token=payload_token).first()
            if intent and not intent.external_id:
                intent.external_id = invoice_id
        if not intent:
            return False, "intent not found"
        if intent.processed_at:
            return True, "already processed"
        if not intent_not_expired(intent, hours=24):
            return False, "intent expired"
        if not crystalpay_invoice_matches_intent(info, intent):
            return False, "intent mismatch"
        user_obj = User.query.get(intent.user_id)
        if not user_obj:
            return False, "user not found"
        create_remnawave_subscription(user_obj, int(intent.plan_months), strict=True)
        apply_coupon_redemption_for_intent(intent)
        apply_referral_bonus_if_eligible(user_obj)
        mark_processed_payment(
            db_session=db.session,
            processed_model=ProcessedPayment,
            provider="crystalpay",
            external_id=invoice_id,
            intent=intent,
        )
        return True, "ok"


def crystalpay_webhook_impl(content: dict) -> tuple[dict, int]:
    invoice_id = str(content.get("id") or "").strip()
    signature = str(content.get("signature") or "").strip()
    state = str(content.get("state") or "").strip()
    extra = content.get("extra")
    if not invoice_id:
        return {"ok": False, "error": "missing id"}, 400
    salt = (os.environ.get("CRYSTALPAY_SALT") or "").strip()
    if salt:
        if not signature:
            return {"ok": False, "error": "missing signature"}, 400
        computed = hashlib.sha1(f"{invoice_id}:{salt}".encode()).hexdigest()
        if not hmac.compare_digest(computed, signature):
            return {"ok": False, "error": "invalid signature"}, 403
    if state != "payed":
        return {"ok": True, "ignored": True}, 200
    try:
        info = crystal_invoice_info(invoice_id)
        if str(info.get("state") or "") != "payed":
            return {"ok": True, "not_final": True}, 200
    except Exception as exc:
        print(f"Crystal Pay info check failed: {exc}")
        if not salt:
            return {"ok": False, "error": "temporary"}, 429
    processed, msg = crystalpay_process_paid_invoice(invoice_id, str(extra or "").strip())
    return {"ok": processed, "message": msg}, 200
