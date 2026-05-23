from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime
from decimal import Decimal

from flask import url_for

from vexnd_app.config import HTTP
from vexnd_app.extensions import db
from vexnd_app.models import PaymentIntent, ProcessedPayment, User
from vexnd_app.plans import format_usd_amount, plan_duration_label, plan_price_usd
from vexnd_app.security.webhooks import intent_not_expired, payment_processing_lock
from vexnd_app.services.coupons import apply_coupon_redemption_for_intent, intent_pricing
from vexnd_app.services.payment_intents import mark_processed_payment
from vexnd_app.services.referrals import apply_referral_bonus_if_eligible
from vexnd_app.services.subscriptions import create_remnawave_subscription


def cryptobot_api_base() -> str:
    is_testnet = os.environ.get("CRYPTO_PAY_TESTNET", "").lower() in ["true", "1", "yes"]
    return "https://testnet-pay.crypt.bot/api" if is_testnet else "https://pay.crypt.bot/api"


def create_crypto_invoice(user_id: int, plan_months: int, *, amount_usd: Decimal | None = None) -> tuple[dict, str]:
    amount = format_usd_amount(amount_usd if amount_usd is not None else plan_price_usd(plan_months))
    token = os.environ.get("CRYPTO_PAY_API_TOKEN")
    if not token:
        raise RuntimeError("Crypto Pay API token not configured")
    intent_token = secrets.token_urlsafe(24)
    callback_url = url_for("cryptobot_return", token=intent_token, _external=True)
    invoice_request = {
        "currency_type": "fiat",
        "fiat": "USD",
        "amount": str(amount),
        "accepted_assets": "USDT,TON,BTC,ETH,LTC,BNB,TRX,USDC",
        "description": f"VEXND subscription for {plan_duration_label(plan_months, 'en')}",
        "payload": intent_token,
        "paid_btn_name": "callback",
        "paid_btn_url": callback_url,
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": 3600,
    }
    headers = {"Crypto-Pay-API-Token": token, "Content-Type": "application/json"}
    resp = HTTP.post(f"{cryptobot_api_base()}/createInvoice", json=invoice_request, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Error creating invoice: {data.get('error')}")
    return data["result"], intent_token


def cryptobot_get_invoice_by_id(invoice_id: str) -> dict | None:
    token_api = (os.environ.get("CRYPTO_PAY_API_TOKEN") or "").strip()
    if not token_api:
        raise RuntimeError("Crypto Pay API token not configured")
    headers = {"Crypto-Pay-API-Token": token_api}
    resp = HTTP.get(f"{cryptobot_api_base()}/getInvoices", headers=headers, params={"invoice_ids": invoice_id}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(data.get("error") if isinstance(data, dict) else "bad response")
    result = data.get("result")
    invoices = []
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        invoices = [x for x in result["items"] if isinstance(x, dict)]
    elif isinstance(result, list):
        invoices = [x for x in result if isinstance(x, dict)]
    return invoices[0] if invoices else None


def cryptobot_verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    token_api = (os.environ.get("CRYPTO_PAY_API_TOKEN") or "").strip()
    signature = (signature or "").strip().lower()
    if not token_api or not signature or not raw_body:
        return False
    secret = hashlib.sha256(token_api.encode("utf-8")).digest()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def cryptobot_invoice_matches_intent(inv: dict, intent: PaymentIntent) -> bool:
    if not isinstance(inv, dict) or not intent:
        return False
    if str(inv.get("payload") or "").strip() != str(intent.token or "").strip():
        return False
    expected_amount = format_usd_amount(intent_pricing(intent)["final_price"])
    inv_amount = str(inv.get("amount") or "").strip()
    if inv_amount and inv_amount != expected_amount:
        return False
    if str(inv.get("currency_type") or "").strip() and str(inv.get("currency_type")).strip() != "fiat":
        return False
    if str(inv.get("fiat") or "").strip() and str(inv.get("fiat")).strip() != "USD":
        return False
    return True


def cryptobot_process_paid_invoice(invoice_id: str, payload_token: str | None = None) -> tuple[bool, str]:
    invoice_id = (invoice_id or "").strip()
    if not invoice_id:
        return False, "missing invoice_id"
    with payment_processing_lock("cryptobot", invoice_id):
        if ProcessedPayment.query.filter_by(provider="cryptobot", external_id=invoice_id).first():
            return True, "duplicate"
        inv = cryptobot_get_invoice_by_id(invoice_id)
        if not inv or str(inv.get("status") or "").strip() != "paid":
            return False, "not paid"
        intent = PaymentIntent.query.filter_by(provider="cryptobot", external_id=invoice_id).first()
        if not intent and payload_token:
            intent = PaymentIntent.query.filter_by(provider="cryptobot", token=payload_token).first()
            if intent and not intent.external_id:
                intent.external_id = invoice_id
        if not intent:
            return False, "intent not found"
        if intent.processed_at:
            return True, "already processed"
        if not intent_not_expired(intent, hours=24):
            return False, "intent expired"
        if not cryptobot_invoice_matches_intent(inv, intent):
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
            provider="cryptobot",
            external_id=invoice_id,
            intent=intent,
        )
        return True, "ok"
