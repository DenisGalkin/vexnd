from __future__ import annotations

import hashlib
import hmac
import os

from flask import flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from app.core.extensions import db
from app.domain.models import PaymentIntent
from app.http.security.webhooks import provider_error_id, secrets_match
from app.services.balance import (
    amount_to_cents,
    can_pay_for_plan_with_balance,
    clamp_topup_cents,
    purchase_subscription_with_balance,
    topup_description,
)
from app.services.coupons import coupon_pricing, create_intent_pricing
from app.services.payment_intents import create_intent_with_pricing
from app.services.payments.crystalpay import create_crystal_invoice, crystal_invoice_info, crystalpay_process_paid_invoice, crystalpay_webhook_impl
from app.services.payments.cryptobot import create_crypto_invoice, cryptobot_process_paid_invoice, cryptobot_verify_webhook_signature
from app.services.payments.heleket import create_heleket_invoice, heleket_process_paid, heleket_webhook_impl
from app.services.payments.platega import create_platega_transaction, platega_process_paid_transaction, platega_webhook_impl
from app.http.helpers import return_intent_visible, return_redirect_target, translate


SUPPORTED_PAYMENT_METHODS = frozenset({"cryptobot", "crystalpay", "platega", "heleket", "balance"})


def normalize_payment_method(raw_method: str | None) -> str:
    return (raw_method or "").strip().lower()


@login_required
def start_payment(method):
    method = normalize_payment_method(method)
    if method not in SUPPORTED_PAYMENT_METHODS:
        return redirect(url_for("coming_soon"))
    try:
        plan_int = int(request.args.get("plan", "1"))
    except ValueError:
        plan_int = 1
    from app.domain.plans import plan_details

    plan_int = plan_details(plan_int)["months"]
    pricing = coupon_pricing(plan_int, request.form.get("coupon_code"), current_user.id if current_user.is_authenticated else None)
    if pricing.get("error"):
        flash(pricing["error"], "error")
        return redirect(url_for("checkout", plan=plan_int))
    amount_usd = pricing["final_price"]
    if method == "balance":
        try:
            purchase_subscription_with_balance(current_user, plan_int, request.form.get("coupon_code"))
            flash(translate("Подписка активирована и оплачена с внутреннего баланса ✅"), "success")
        except ValueError as exc:
            if str(exc) == "insufficient_balance":
                flash(translate("Недостаточно средств на внутреннем балансе."), "error")
            else:
                flash(str(exc), "error")
        except Exception:
            db.session.rollback()
            flash(translate("Не удалось списать средства с баланса. Попробуйте позже."), "error")
        return redirect(url_for("dashboard"))
    try:
        if method == "cryptobot":
            invoice, intent_token = create_crypto_invoice(current_user.id, plan_int, amount_usd=amount_usd)
            inv_id = str(invoice.get("invoice_id") or invoice.get("invoiceId") or invoice.get("id") or "").strip()
            create_intent_with_pricing(db_session=db.session, intent_model=PaymentIntent, create_pricing_fn=create_intent_pricing, provider="cryptobot", token=intent_token, user_id=current_user.id, plan_months=plan_int, purpose="subscription", balance_amount_cents=None, external_id=inv_id or None, pricing=pricing)
            return redirect(invoice.get("bot_invoice_url") or invoice.get("pay_url"))
        if method == "crystalpay":
            invoice, intent_token = create_crystal_invoice(current_user.id, plan_int, amount_usd=amount_usd)
            inv_id = str(invoice.get("id") or "").strip()
            create_intent_with_pricing(db_session=db.session, intent_model=PaymentIntent, create_pricing_fn=create_intent_pricing, provider="crystalpay", token=intent_token, user_id=current_user.id, plan_months=plan_int, purpose="subscription", balance_amount_cents=None, external_id=inv_id or None, pricing=pricing)
            return redirect(invoice.get("url"))
        if method == "platega":
            invoice, intent_token, payment_details = create_platega_transaction(current_user.id, plan_int, amount_usd=amount_usd)
            tx_id = str(invoice.get("transactionId") or invoice.get("transaction_id") or invoice.get("id") or "").strip()
            create_intent_with_pricing(
                db_session=db.session,
                intent_model=PaymentIntent,
                create_pricing_fn=create_intent_pricing,
                provider="platega",
                token=intent_token,
                user_id=current_user.id,
                plan_months=plan_int,
                purpose="subscription",
                balance_amount_cents=None,
                external_id=tx_id or None,
                pricing=pricing,
                expected_provider_amount=payment_details["amount"],
                expected_provider_currency=payment_details["currency"],
            )
            return redirect((invoice.get("url") or invoice.get("redirect") or invoice.get("payformSuccessUrl") or "").strip())
        if method == "heleket":
            invoice, intent_token = create_heleket_invoice(current_user.id, plan_int, amount_usd=amount_usd)
            ext_id = str(invoice.get("uuid") or invoice.get("order_id") or invoice.get("id") or "").strip()
            create_intent_with_pricing(db_session=db.session, intent_model=PaymentIntent, create_pricing_fn=create_intent_pricing, provider="heleket", token=intent_token, user_id=current_user.id, plan_months=plan_int, purpose="subscription", balance_amount_cents=None, external_id=ext_id, pricing=pricing)
            return redirect(str(invoice.get("url") or "").strip())
    except Exception as exc:
        if method in {"platega", "heleket"}:
            flash(translate("Не удалось создать платёж. Код ошибки: ") + provider_error_id(method, exc), "error")
            return redirect(url_for("checkout", plan=plan_int))
        return redirect(url_for("coming_soon"))
    return redirect(url_for("coming_soon"))


@login_required
def start_balance_payment(method):
    method = normalize_payment_method(method)
    if method not in SUPPORTED_PAYMENT_METHODS or method == "balance":
        return redirect(url_for("coming_soon"))
    raw_amount = (request.form.get("amount") or request.args.get("amount") or "").strip()
    try:
        amount_cents = clamp_topup_cents(amount_to_cents(raw_amount or "0"))
    except Exception:
        flash(translate("Укажите корректную сумму пополнения."), "error")
        return redirect(url_for("dashboard"))
    amount_usd = amount_cents / 100
    pricing = {
        "coupon_code": None,
        "coupon_applied": False,
        "original_price": amount_usd,
        "final_price": amount_usd,
        "discount_amount": 0,
    }
    description = topup_description(amount_cents)
    try:
        if method == "cryptobot":
            invoice, intent_token = create_crypto_invoice(current_user.id, 0, amount_usd=amount_usd, description=description)
            external_id = str(invoice.get("invoice_id") or invoice.get("invoiceId") or invoice.get("id") or "").strip()
            pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
        elif method == "crystalpay":
            invoice, intent_token = create_crystal_invoice(current_user.id, 0, amount_usd=amount_usd, description=description)
            external_id = str(invoice.get("id") or "").strip()
            pay_url = invoice.get("url")
        elif method == "platega":
            invoice, intent_token, payment_details = create_platega_transaction(current_user.id, 0, amount_usd=amount_usd, description=description)
            external_id = str(invoice.get("transactionId") or invoice.get("transaction_id") or invoice.get("id") or "").strip()
            pay_url = (invoice.get("url") or invoice.get("redirect") or invoice.get("payformSuccessUrl") or "").strip()
        else:
            invoice, intent_token = create_heleket_invoice(current_user.id, 0, amount_usd=amount_usd, description=description)
            external_id = str(invoice.get("uuid") or invoice.get("order_id") or invoice.get("id") or "").strip()
            pay_url = str(invoice.get("url") or "").strip()
        create_intent_with_pricing(
            db_session=db.session,
            intent_model=PaymentIntent,
            create_pricing_fn=create_intent_pricing,
            provider="crystalpay" if method == "crystalpay" else method,
            token=intent_token,
            user_id=current_user.id,
            plan_months=0,
            purpose="balance_topup",
            balance_amount_cents=amount_cents,
            external_id=external_id or None,
            pricing=pricing,
            expected_provider_amount=(payment_details["amount"] if method == "platega" else None),
            expected_provider_currency=(payment_details["currency"] if method == "platega" else None),
        )
        return redirect(pay_url)
    except Exception as exc:
        db.session.rollback()
        print(f"Balance top-up creation failed: {exc}")
        flash(translate("Не удалось создать пополнение баланса. Попробуйте позже."), "error")
        return redirect(url_for("dashboard"))


# The legacy /payment_callback endpoint has been removed.
# It previously returned HTTP 410 but served no purpose. Removing unused routes
# reduces attack surface and avoids confusion.


def _return_by_provider(provider: str, process_fn):
    token = (request.args.get("token") or "").strip()
    if not token:
        flash(translate("Если вы оплатили — подождите 5–20 секунд и обновите кабинет. Подписка появится автоматически."), "info")
        return redirect(return_redirect_target())
    intent = PaymentIntent.query.filter_by(provider=provider, token=token).first()
    if not return_intent_visible(intent):
        flash(translate("Если вы оплатили — подождите 5–20 секунд и обновите кабинет. Подписка появится автоматически."), "info")
        return redirect(return_redirect_target())
    if intent.processed_at:
        if not current_user.is_authenticated:
            flash(translate("Оплата уже подтверждена. Войдите в аккаунт, чтобы открыть кабинет."), "success")
        return redirect(return_redirect_target())
    ext_id = (intent.external_id or "").strip()
    if not ext_id:
        flash(translate("Не удалось определить ID счёта. Напишите в поддержку."), "error")
        return redirect(return_redirect_target())
    try:
        ok, msg = process_fn(ext_id, token)
        if ok:
            if msg == "ok":
                flash(translate("Подписка активирована ✅"), "success")
            elif not current_user.is_authenticated:
                flash(translate("Оплата подтверждена. Войдите в аккаунт, чтобы открыть кабинет."), "success")
            return redirect(return_redirect_target())
        if "not paid" in msg or msg == "not confirmed":
            flash(translate("Платёж ещё не подтверждён. Если вы оплатили — подождите немного и обновите кабинет."), "info")
            return redirect(return_redirect_target())
        if msg == "intent mismatch":
            flash(translate("Платёж найден, но данные счёта не совпадают. Напишите в поддержку."), "error")
            return redirect(return_redirect_target())
    except Exception as exc:
        print(f"{provider} return error: {exc}")
    flash(translate("Спасибо! Мы проверяем оплату. Если статус не обновился, подождите пару минут и обновите страницу."), "info")
    return redirect(return_redirect_target())


def cryptobot_return():
    return _return_by_provider("cryptobot", lambda ext_id, token: cryptobot_process_paid_invoice(ext_id, token))


def crystalpay_return():
    return _return_by_provider("crystalpay", lambda ext_id, token: crystalpay_process_paid_invoice(ext_id, token))


def cryptobot_webhook():
    secret = os.environ.get("CRYPTOBOT_WEBHOOK_PATH_SECRET", "").strip()
    if secret:
        return jsonify({"ok": False, "error": "not found"}), 404
    return _cryptobot_webhook_impl()


def cryptobot_webhook_secret(secret: str):
    expected = os.environ.get("CRYPTOBOT_WEBHOOK_PATH_SECRET", "").strip()
    if not secrets_match(secret, expected):
        return jsonify({"ok": False, "error": "not found"}), 404
    return _cryptobot_webhook_impl()


def _cryptobot_webhook_impl():
    try:
        raw_body = request.get_data(cache=True)
        signature = request.headers.get("crypto-pay-api-signature")
        if not cryptobot_verify_webhook_signature(raw_body, signature):
            return jsonify({"ok": False, "error": "invalid signature"}), 403
        content = request.get_json(silent=True)
        if not isinstance(content, dict):
            return jsonify({"ok": False, "error": "bad json"}), 400
        update_type = str(content.get("update_type") or content.get("updateType") or "").strip()
        payload = content.get("payload")
        inv = payload if isinstance(payload, dict) else {}
        if update_type and update_type != "invoice_paid":
            return jsonify({"ok": True, "ignored": True}), 200
        invoice_id = str(inv.get("invoice_id") or inv.get("invoiceId") or inv.get("id") or "").strip()
        payload_token = str(inv.get("payload") or "").strip() or None
        processed, msg = cryptobot_process_paid_invoice(invoice_id, payload_token)
        return jsonify({"ok": processed, "message": msg}), 200
    except Exception as exc:
        print(f"Crypto Pay webhook error: {exc}")
        return jsonify({"ok": False, "error": "temporary"}), 429


def heleket_return():
    token = (request.args.get("token") or "").strip()
    if not token:
        flash(translate("Если вы оплатили — подождите 5–20 секунд и обновите кабинет. Подписка появится автоматически."), "info")
        return redirect(url_for("dashboard"))
    intent = PaymentIntent.query.filter_by(provider="heleket", token=token).first()
    if not return_intent_visible(intent):
        flash(translate("Если вы оплатили — подождите 5–20 секунд и обновите кабинет. Подписка появится автоматически."), "info")
        return redirect(url_for("dashboard"))
    if intent.processed_at:
        return redirect(url_for("dashboard"))
    ext = (intent.external_id or "").strip()
    if not ext:
        flash(translate("Не удалось определить ID счёта. Напишите в поддержку."), "error")
        return redirect(url_for("dashboard"))
    try:
        ok, msg = heleket_process_paid(ext=ext, token=token)
        if ok:
            if msg == "ok":
                flash(translate("Подписка активирована ✅"), "success")
            return redirect(url_for("dashboard"))
        if msg.startswith("not paid"):
            flash(translate("Платёж ещё не подтверждён. Если вы оплатили — подождите немного и обновите кабинет."), "info")
            return redirect(url_for("dashboard"))
    except Exception as exc:
        print(f"Heleket return verify error: {exc}")
    flash(translate("Спасибо! Мы проверяем оплату. Если статус не обновился, подождите пару минут и обновите страницу."), "info")
    return redirect(url_for("dashboard"))


def heleket_webhook():
    expected = os.environ.get("HELEKET_WEBHOOK_PATH_SECRET", "").strip()
    if expected:
        return jsonify({"ok": False, "error": "not found"}), 404
    return heleket_webhook_impl()


def heleket_webhook_secret(secret: str):
    expected = os.environ.get("HELEKET_WEBHOOK_PATH_SECRET", "").strip()
    if not secrets_match(secret, expected):
        return jsonify({"ok": False, "error": "not found"}), 404
    return heleket_webhook_impl()


def crystalpay_webhook():
    expected = os.environ.get("CRYSTALPAY_WEBHOOK_PATH_SECRET", "").strip()
    if expected:
        return jsonify({"ok": False, "error": "not found"}), 404
    return _crystalpay_webhook_impl()


def crystalpay_webhook_secret(secret: str):
    expected = os.environ.get("CRYSTALPAY_WEBHOOK_PATH_SECRET", "").strip()
    if not secrets_match(secret, expected):
        return jsonify({"ok": False, "error": "not found"}), 404
    return _crystalpay_webhook_impl()


def _crystalpay_webhook_impl():
    content = request.get_json(silent=True)
    if not isinstance(content, dict):
        content = dict(request.form) if request.form else {}
    response, status = crystalpay_webhook_impl(content)
    return jsonify(response), status


def platega_return():
    return _return_by_provider("platega", lambda ext_id, token: platega_process_paid_transaction(ext_id, token))


@login_required
def platega_fail():
    flash(translate("Оплата не прошла или была отменена."), "error")
    return redirect(url_for("checkout", plan=request.args.get("plan") or "1"))


def platega_webhook():
    expected = os.environ.get("PLATEGA_WEBHOOK_PATH_SECRET", "").strip()
    if expected:
        return jsonify({"ok": False, "error": "not found"}), 404
    return platega_webhook_impl()


def platega_callback():
    return platega_webhook()


def platega_webhook_secret(secret: str):
    expected = os.environ.get("PLATEGA_WEBHOOK_PATH_SECRET", "").strip()
    if not expected or not hmac.compare_digest(secret, expected):
        return jsonify({"ok": False, "error": "not found"}), 404
    return platega_webhook_impl()


def platega_callback_secret(secret: str):
    return platega_webhook_secret(secret)


def register(app) -> None:
    app.add_url_rule("/start_payment/<method>", endpoint="start_payment", view_func=start_payment, methods=["POST"])
    app.add_url_rule("/start_balance_payment/<method>", endpoint="start_balance_payment", view_func=start_balance_payment, methods=["POST"])
    # Removed deprecated /payment_callback endpoint registration.
    app.add_url_rule("/cryptobot/return", endpoint="cryptobot_return", view_func=cryptobot_return, methods=["GET"])
    app.add_url_rule("/crystalpay/return", endpoint="crystalpay_return", view_func=crystalpay_return, methods=["GET"])
    app.add_url_rule("/cryptobot/webhook", endpoint="cryptobot_webhook", view_func=cryptobot_webhook, methods=["POST"])
    app.add_url_rule("/cryptobot/webhook/<secret>", endpoint="cryptobot_webhook_secret", view_func=cryptobot_webhook_secret, methods=["POST"])
    app.add_url_rule("/heleket/return", endpoint="heleket_return", view_func=heleket_return, methods=["GET"])
    app.add_url_rule("/heleket/webhook", endpoint="heleket_webhook", view_func=heleket_webhook, methods=["POST"])
    app.add_url_rule("/heleket/webhook/<secret>", endpoint="heleket_webhook_secret", view_func=heleket_webhook_secret, methods=["POST"])
    app.add_url_rule("/crystalpay/webhook", endpoint="crystalpay_webhook", view_func=crystalpay_webhook, methods=["POST"])
    app.add_url_rule("/crystalpay/webhook/<secret>", endpoint="crystalpay_webhook_secret", view_func=crystalpay_webhook_secret, methods=["POST"])
    app.add_url_rule("/platega/return", endpoint="platega_return", view_func=platega_return, methods=["GET"])
    app.add_url_rule("/platega/fail", endpoint="platega_fail", view_func=platega_fail, methods=["GET"])
    app.add_url_rule("/platega/webhook", endpoint="platega_webhook", view_func=platega_webhook, methods=["GET", "HEAD", "POST"])
    app.add_url_rule("/platega/callback", endpoint="platega_callback", view_func=platega_callback, methods=["GET", "HEAD", "POST"])
    app.add_url_rule("/platega/webhook/<secret>", endpoint="platega_webhook_secret", view_func=platega_webhook_secret, methods=["GET", "HEAD", "POST"])
    app.add_url_rule("/platega/callback/<secret>", endpoint="platega_callback_secret", view_func=platega_callback_secret, methods=["GET", "HEAD", "POST"])
