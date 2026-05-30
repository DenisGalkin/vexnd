from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime
from decimal import Decimal
from requests import HTTPError

from flask import jsonify, request

from app.core.config import HTTP
from app.core.extensions import db
from app.domain.models import PaymentIntent, ProcessedPayment, User
from app.domain.plans import format_usd_amount, to_decimal_amount
from app.http.security.webhooks import intent_not_expired, payment_processing_lock
from app.services.coupons import apply_coupon_redemption_for_intent, intent_expected_amounts
from app.services.referrals import apply_referral_bonus_if_eligible
from app.services.security import get_webhook_secret
from app.services.balance import fulfill_payment_intent
from app.services.subscriptions import create_remnawave_subscription
from app.http.helpers import public_url


def platega_target_currency() -> str:
    return (os.environ.get("PLATEGA_CURRENCY") or "RUB").strip().upper() or "RUB"


def platega_credentials() -> tuple[str, str]:
    merchant_id = (os.environ.get("PLATEGA_MERCHANT_ID") or "").strip()
    secret = (os.environ.get("PLATEGA_SECRET") or "").strip()
    if not merchant_id or not secret:
        raise RuntimeError("Platega credentials not configured")
    return merchant_id, secret


def platega_api_base() -> str:
    return (os.environ.get("PLATEGA_API_BASE") or "https://gate.platega.io/api/v2").strip().rstrip("/")


def platega_raise_for_status(resp) -> None:
    try:
        resp.raise_for_status()
    except HTTPError as exc:
        details = ""
        try:
            payload = resp.json()
            details = str(payload)
        except Exception:
            details = (resp.text or "").strip()
        if details:
            raise RuntimeError(f"Platega API error {resp.status_code}: {details}") from exc
        raise


def platega_get_rate(payment_method: int, currency_from: str, currency_to: str) -> float:
    merchant_id, secret = platega_credentials()
    headers = {"X-MerchantId": merchant_id, "X-Secret": secret, "Accept": "application/json"}
    params = {
        "merchantId": merchant_id,
        "paymentMethod": payment_method,
        "currencyFrom": currency_from.upper(),
        "currencyTo": currency_to.upper(),
    }
    resp = HTTP.get(f"{platega_api_base()}/rates/payment_method_rate", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json() or {}
    return float(data.get("rate"))


def platega_quote_amount(amount_usd: Decimal | float | int | str, *, payment_method: int | None = None, currency: str | None = None) -> tuple[float, str, int]:
    price_usd = to_decimal_amount(amount_usd)
    target_currency = (currency or platega_target_currency()).strip().upper() or "RUB"
    method = int(payment_method if payment_method is not None else os.environ.get("PLATEGA_PAYMENT_METHOD", "2"))
    if target_currency == "USD":
        return round(float(price_usd), 2), target_currency, method
    converted_amount = None
    try:
        rate = platega_get_rate(method, "USD", target_currency)
        converted_amount = float(price_usd) * float(rate)
    except Exception:
        for fallback_name in (f"PLATEGA_USD_TO_{target_currency}_RATE", f"PLATEGA_USD_TO_{target_currency}"):
            fallback_rate = os.environ.get(fallback_name)
            if not fallback_rate:
                continue
            try:
                converted_amount = float(price_usd) * float(fallback_rate)
                break
            except Exception:
                converted_amount = None
    if converted_amount is None:
        converted_amount = float(price_usd) * 100.0
    return round(converted_amount, 2), target_currency, method


def create_platega_transaction(
    user_id: int,
    plan_months: int,
    *,
    amount_usd: Decimal | None = None,
    description: str | None = None,
) -> tuple[dict, str]:
    merchant_id, secret = platega_credentials()
    intent_token = secrets.token_urlsafe(24)
    quoted_amount, currency, payment_method = platega_quote_amount(amount_usd)
    webhook_secret = get_webhook_secret("PLATEGA_WEBHOOK_PATH_SECRET")
    payload = {
        "paymentMethod": payment_method,
        "paymentDetails": {
            "amount": quoted_amount,
            "currency": currency,
        },
        "return": public_url("platega_return", token=intent_token),
        "failedUrl": public_url("platega_fail", plan=plan_months),
        "payload": intent_token,
    }
    callback_url = public_url("platega_webhook_secret", secret=webhook_secret) if webhook_secret else public_url("platega_webhook")
    if callback_url:
        payload["callbackUrl"] = callback_url
    if description:
        payload["description"] = description
    headers = {"X-MerchantId": merchant_id, "X-Secret": secret, "Content-Type": "application/json", "Accept": "application/json"}
    resp = HTTP.post(f"{platega_api_base()}/transaction/process", headers=headers, json=payload, timeout=30)
    platega_raise_for_status(resp)
    data = resp.json() or {}
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Platega response")
    return data, intent_token


def platega_get_transaction(tx_id: str) -> dict | None:
    merchant_id, secret = platega_credentials()
    headers = {"X-MerchantId": merchant_id, "X-Secret": secret, "Accept": "application/json"}
    resp = HTTP.get(f"{platega_api_base()}/transaction/{tx_id}", headers=headers, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json() or {}
    return data if isinstance(data, dict) else None


def platega_transaction_matches_intent(tx: dict, intent: PaymentIntent) -> bool:
    if not isinstance(tx, dict) or not intent:
        return False
    if str(tx.get("payload") or "").strip() and str(tx.get("payload")).strip() != str(intent.token or "").strip():
        return False
    amount = tx.get("amount")
    currency = str(tx.get("currency") or "").strip().upper()
    if currency == "USD":
        try:
            paid_amount = Decimal(str(amount or 0))
        except Exception:
            return False
        expected_amounts = intent_expected_amounts(intent)
        if expected_amounts and all(abs(paid_amount - value) > Decimal("0.000000001") for value in expected_amounts):
            return False
    return True


def platega_process_paid_transaction(tx_id: str, payload_token: str | None = None) -> tuple[bool, str]:
    tx_id = (tx_id or "").strip()
    payload_token = (payload_token or "").strip() or None
    if not tx_id:
        return False, "missing transaction id"
    with payment_processing_lock("platega", tx_id):
        if ProcessedPayment.query.filter_by(provider="platega", external_id=tx_id).first():
            return True, "duplicate"
        tx = platega_get_transaction(tx_id)
        if not tx:
            return False, "not found"
        status = str(tx.get("status") or "").strip().upper()
        if status != "CONFIRMED":
            return False, "not confirmed"
        intent = PaymentIntent.query.filter_by(provider="platega", external_id=tx_id).first()
        if not intent and payload_token:
            intent = PaymentIntent.query.filter_by(provider="platega", token=payload_token).first()
            if intent and not intent.external_id:
                intent.external_id = tx_id
        if not intent:
            return False, "intent not found"
        if intent.processed_at:
            return True, "already processed"
        if not intent_not_expired(intent, hours=24):
            return False, "intent expired"
        if not platega_transaction_matches_intent(tx, intent):
            return False, "intent mismatch"
        user_obj = User.query.get(intent.user_id)
        if not user_obj:
            return False, "user not found"
        fulfill_payment_intent(intent, user_obj, tx_id)
        db.session.add(ProcessedPayment(provider="platega", external_id=tx_id))
        intent.processed_at = datetime.utcnow()
        db.session.commit()
        return True, "ok"


def platega_webhook_impl():
    try:
        if request.method in ("GET", "HEAD"):
            return ("", 200)
        try:
            merchant_id, secret = platega_credentials()
        except Exception:
            return jsonify({"ok": False, "error": "credentials misconfigured"}), 500
        header_mid = (request.headers.get("X-MerchantId") or "").strip()
        header_secret = (request.headers.get("X-Secret") or "").strip()
        content = request.get_json(silent=True)
        if not isinstance(content, dict):
            content = {}
        tx_id = str(content.get("id") or "").strip()
        status = str(content.get("status") or "").strip().upper()
        payload_token = str(content.get("payload") or "").strip() or None
        if not tx_id or not status:
            return jsonify({"ok": True, "ignored": True}), 200
        if not header_mid or not header_secret:
            return jsonify({"ok": False, "error": "missing headers"}), 403
        if not (hmac.compare_digest(header_mid, merchant_id) and hmac.compare_digest(header_secret, secret)):
            return jsonify({"ok": False, "error": "invalid headers"}), 403
        if status != "CONFIRMED":
            return jsonify({"ok": True, "ignored": True}), 200
        processed, msg = platega_process_paid_transaction(tx_id, payload_token)
        return jsonify({"ok": processed, "message": msg}), 200
    except Exception as exc:
        print(f"Platega webhook error: {exc}")
        return jsonify({"ok": False, "error": "temporary"}), 429
