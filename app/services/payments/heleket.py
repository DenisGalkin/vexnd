from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from decimal import Decimal

from flask import jsonify, request

from app.core.config import HTTP, _env_bool
from app.core.extensions import db
from app.domain.models import PaymentIntent, ProcessedPayment, User
from app.domain.plans import format_usd_amount, plan_price_usd
from app.http.security.webhooks import payment_processing_lock
from app.services.coupons import apply_coupon_redemption_for_intent, intent_expected_amounts
from app.services.referrals import apply_referral_bonus_if_eligible
from app.services.remnawave import get_remnawave_config
from app.services.security import client_ip, get_webhook_secret
from app.services.balance import fulfill_payment_intent
from app.services.subscriptions import create_remnawave_subscription
from app.http.helpers import public_url


def heleket_credentials() -> tuple[str, str]:
    merchant_id = (os.environ.get("HELEKET_MERCHANT_ID") or "").strip()
    api_key = (os.environ.get("HELEKET_API_KEY") or "").strip()
    if not merchant_id or not api_key:
        raise RuntimeError("Heleket credentials not configured")
    return merchant_id, api_key


def heleket_json_dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("/", "\\/")


def heleket_sign_payload(payload: dict, api_key: str) -> str:
    body = heleket_json_dumps(payload)
    b64 = base64.b64encode(body.encode("utf-8")).decode("utf-8")
    return hashlib.md5((b64 + api_key).encode("utf-8")).hexdigest()


def create_heleket_invoice(
    user_id: int,
    plan_months: int,
    *,
    amount_usd: Decimal | None = None,
    description: str | None = None,
) -> tuple[dict, str]:
    amount = format_usd_amount(amount_usd if amount_usd is not None else plan_price_usd(plan_months))
    merchant_id, api_key = heleket_credentials()
    intent_token = secrets.token_urlsafe(24)
    order_id = secrets.token_hex(16)
    wh_secret = get_webhook_secret("HELEKET_WEBHOOK_PATH_SECRET")
    if wh_secret:
        url_callback = public_url("heleket_webhook_secret", secret=wh_secret)
    else:
        url_callback = public_url("heleket_webhook")
    url_return = public_url("heleket_return", token=intent_token)
    payload = {
        "amount": str(amount),
        "currency": "USD",
        "order_id": order_id,
        "url_callback": url_callback,
        "url_return": url_return,
        "url_success": url_return,
        "additional_data": intent_token,
    }
    if description:
        payload["description"] = description
    sign = heleket_sign_payload(payload, api_key)
    headers = {"merchant": merchant_id, "sign": sign, "Content-Type": "application/json", "Accept": "application/json"}
    body = heleket_json_dumps(payload)
    resp = HTTP.post("https://api.heleket.com/v1/payment", data=body.encode("utf-8"), headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json() or {}
    invoice = data.get("result") if isinstance(data, dict) and isinstance(data.get("result"), dict) else data
    if not isinstance(invoice, dict):
        raise RuntimeError("Unexpected Heleket response")
    return invoice, intent_token


def heleket_payment_info(uuid: str | None = None, order_id: str | None = None) -> dict | None:
    if not uuid and not order_id:
        return None
    merchant_id, api_key = heleket_credentials()
    payload: dict[str, str] = {}
    if order_id and not uuid:
        payload["order_id"] = str(order_id)
    if uuid:
        payload["uuid"] = str(uuid)
    sign = heleket_sign_payload(payload, api_key)
    headers = {"merchant": merchant_id, "sign": sign, "Content-Type": "application/json", "Accept": "application/json"}
    body = heleket_json_dumps(payload)
    resp = HTTP.post("https://api.heleket.com/v1/payment/info", data=body.encode("utf-8"), headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json() or {}
    inv = data.get("result") if isinstance(data, dict) and isinstance(data.get("result"), dict) else data
    return inv if isinstance(inv, dict) else None


def heleket_invoice_matches_intent(inv: dict, intent: PaymentIntent) -> bool:
    if not isinstance(inv, dict) or not intent:
        return False
    token = str(inv.get("additional_data") or "").strip()
    if token and token != str(intent.token or "").strip():
        return False
    currency = str(inv.get("currency") or "").strip().upper()
    if currency and currency != "USD":
        return False
    expected_amounts = intent_expected_amounts(intent)
    try:
        paid_amount = float(inv.get("amount") or 0)
    except Exception:
        paid_amount = None
    if paid_amount is None or not expected_amounts:
        return paid_amount is None
    return any(abs(Decimal(str(paid_amount)) - amount) <= Decimal("0.000000001") for amount in expected_amounts)


def heleket_process_paid(ext: str | None = None, *, uuid: str | None = None, order_id: str | None = None, token: str | None = None) -> tuple[bool, str]:
    ext = (ext or "").strip() or None
    uuid = (uuid or "").strip() or None
    order_id = (order_id or "").strip() or None
    token = (token or "").strip() or None
    lock_id = uuid or order_id or ext or token
    if not lock_id:
        return False, "missing invoice id"
    with payment_processing_lock("heleket", lock_id):
        known_ids = {i for i in (uuid, order_id, ext) if i}
        if known_ids and ProcessedPayment.query.filter(ProcessedPayment.provider == "heleket", ProcessedPayment.external_id.in_(list(known_ids))).first():
            return True, "duplicate"
        intent = None
        if token:
            intent = PaymentIntent.query.filter_by(provider="heleket", token=token).first()
        if not intent:
            for value in (uuid, order_id, ext):
                if value:
                    intent = PaymentIntent.query.filter_by(provider="heleket", external_id=value).first()
                    if intent:
                        break
        inv = None
        lookup_uuid = uuid or (ext if ext and "-" in ext else None)
        lookup_order = order_id or (ext if ext and "-" not in ext else None)
        try:
            inv = heleket_payment_info(uuid=lookup_uuid) if lookup_uuid else None
            if not inv and lookup_order:
                inv = heleket_payment_info(order_id=lookup_order)
        except Exception as exc:
            print(f"Heleket payment info fetch error: {exc}")
        if isinstance(inv, dict):
            uuid = uuid or (str(inv.get("uuid") or "").strip() or None)
            order_id = order_id or (str(inv.get("order_id") or "").strip() or None)
            token = token or (str(inv.get("additional_data") or "").strip() or None)
        if not intent and token:
            intent = PaymentIntent.query.filter_by(provider="heleket", token=token).first()
        if not intent:
            for value in (uuid, order_id):
                if value:
                    intent = PaymentIntent.query.filter_by(provider="heleket", external_id=value).first()
                    if intent:
                        break
        if not intent:
            return False, "intent not found"
        if intent.processed_at:
            return True, "already processed"
        if intent.created_at and intent.created_at < (datetime.utcnow() - timedelta(hours=24)):
            return False, "intent expired"
        if not isinstance(inv, dict):
            inv = heleket_payment_info(uuid=uuid) or heleket_payment_info(order_id=order_id) or heleket_payment_info(uuid=ext) or heleket_payment_info(order_id=ext)
        if not isinstance(inv, dict):
            return False, "invoice not found"
        status = str(inv.get("status") or inv.get("payment_status") or "").strip().lower()
        if status not in ("paid", "paid_over"):
            return False, f"not paid ({status})"
        if not heleket_invoice_matches_intent(inv, intent):
            return False, "intent mismatch"
        user_obj = User.query.get(intent.user_id)
        if not user_obj:
            return False, "user not found"
        fulfill_payment_intent(intent, user_obj, uuid or order_id or ext or intent.external_id or "")
        ids_to_mark = {i for i in (uuid, order_id, ext, intent.external_id) if i}
        for pid in ids_to_mark:
            if not ProcessedPayment.query.filter_by(provider="heleket", external_id=pid).first():
                db.session.add(ProcessedPayment(provider="heleket", external_id=pid))
        if uuid and intent.external_id != uuid:
            intent.external_id = uuid
        intent.processed_at = datetime.utcnow()
        db.session.commit()
        return True, "ok"


def heleket_webhook_impl():
    try:
        if _env_bool("HELEKET_WEBHOOK_DEBUG", False):
            try:
                print(f"[Heleket] webhook hit path={request.path} ip={client_ip()}")
            except Exception:
                pass
        if _env_bool("HELEKET_WEBHOOK_REQUIRE_IP_WHITELIST", False):
            allowed = (os.environ.get("HELEKET_WEBHOOK_ALLOWED_IPS") or "31.133.220.8").strip()
            allowed_set = {ip.strip() for ip in allowed.split(",") if ip.strip()}
            ip = (client_ip() or "").strip()
            if ip not in allowed_set:
                return jsonify({"ok": False, "error": "ip not allowed"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "bad json"}), 400
        sign = str(data.get("sign") or "").strip()
        if not sign:
            return jsonify({"ok": False, "error": "missing sign"}), 400
        _merchant_id, api_key = heleket_credentials()
        copy = dict(data)
        copy.pop("sign", None)
        expected = heleket_sign_payload(copy, api_key)
        if not hmac.compare_digest(expected, sign):
            return jsonify({"ok": False, "error": "invalid signature"}), 403
        status = str(copy.get("status") or "").strip().lower()
        if status not in ("paid", "paid_over"):
            return jsonify({"ok": True, "ignored": True}), 200
        ok, msg = heleket_process_paid(
            ext=str(copy.get("uuid") or copy.get("order_id") or "").strip() or None,
            uuid=str(copy.get("uuid") or "").strip() or None,
            order_id=str(copy.get("order_id") or "").strip() or None,
            token=str(copy.get("additional_data") or "").strip() or None,
        )
        return jsonify({"ok": ok, "message": msg}), 200
    except Exception as exc:
        print(f"Heleket webhook error: {exc}")
        return jsonify({"ok": False, "error": "temporary"}), 429
