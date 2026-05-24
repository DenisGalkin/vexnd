from __future__ import annotations

import os
import secrets
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.bot.common import (
    BALANCE_PROVIDERS,
    BOT_PLAN_CATALOG,
    PAYMENT_METHODS,
    BotBalanceTopup,
    BotUserState,
    HTTP,
    bot_plan_label,
    bot_plan_price_usd,
    bot_webhook_url,
    create_bot_intent_pricing,
    db,
    edit_message,
    ensure_bot_intent_pricing,
    h,
    money,
    money_amount,
    t,
    utc_now,
)
from app.bot.keyboards import (
    keyboard,
    main_menu,
    payment_link_keyboard,
    payment_methods_keyboard,
    plans_keyboard,
    profile_keyboard,
    qr_keyboard,
    subscription_keyboard,
    topup_amounts_keyboard,
    topup_payment_methods_keyboard,
)
from app.bot.subscriptions import format_subscription, invalidate_remnawave_snapshot
from app.domain.models import PaymentIntent, ProcessedPayment, User
from app.http.security.webhooks import payment_processing_lock
from app.services.payments.crystalpay import crystal_credentials, crystal_invoice_info, crystalpay_process_paid_invoice
from app.services.payments.cryptobot import cryptobot_api_base, cryptobot_get_invoice_by_id, cryptobot_process_paid_invoice
from app.services.payments.heleket import heleket_credentials, heleket_json_dumps, heleket_payment_info, heleket_process_paid, heleket_sign_payload
from app.services.payments.platega import platega_api_base, platega_credentials, platega_get_transaction, platega_process_paid_transaction, platega_quote_amount
from app.services.referrals import apply_referral_bonus_if_eligible
from app.services.security import get_webhook_secret
from app.services.subscriptions import create_remnawave_subscription


def create_bot_crypto_invoice(plan_months: int = 0, amount_cents: int | None = None, description: str | None = None) -> tuple[dict[str, object], str]:
    token = (os.environ.get("CRYPTO_PAY_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Crypto Pay API token not configured")
    intent_token = secrets.token_urlsafe(24)
    amount_usd = (amount_cents / 100) if amount_cents is not None else bot_plan_price_usd(plan_months)
    payload = {
        "currency_type": "fiat",
        "fiat": "USD",
        "amount": f"{amount_usd:g}",
        "accepted_assets": "USDT,TON,BTC,ETH,LTC,BNB,TRX,USDC",
        "description": description or f"VEXND subscription for {plan_months} month(s)",
        "payload": intent_token,
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": 3600,
    }
    headers = {"Crypto-Pay-API-Token": token, "Content-Type": "application/json"}
    resp = HTTP.post(f"{cryptobot_api_base()}/createInvoice", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"Error creating Crypto Bot invoice: {data.get('error') if isinstance(data, dict) else data}")
    return data["result"], intent_token


def create_bot_crystal_invoice(user: User, plan_months: int = 0, amount_cents: int | None = None, description: str | None = None, include_callback: bool = True) -> tuple[dict[str, object], str]:
    auth_login, auth_secret = crystal_credentials()
    intent_token = secrets.token_urlsafe(24)
    amount_usd = (amount_cents / 100) if amount_cents is not None else bot_plan_price_usd(plan_months)
    payload = {
        "auth_login": auth_login,
        "auth_secret": auth_secret,
        "amount": float(amount_usd),
        "currency": "USD",
        "type": "purchase",
        "lifetime": 60,
        "description": description or f"VEXND subscription for {plan_months} month(s)",
        "extra": intent_token,
    }
    if include_callback:
        cp_wh_secret = get_webhook_secret("CRYSTALPAY_WEBHOOK_PATH_SECRET")
        payload["callback_url"] = bot_webhook_url("crystalpay_webhook_secret", secret=cp_wh_secret) if cp_wh_secret else bot_webhook_url("crystalpay_webhook")
    payload = {k: v for k, v in payload.items() if v is not None}
    resp = HTTP.post("https://api.crystalpay.io/v3/invoice/create/", json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(f"Error creating Crystal Pay invoice: {data.get('errors') if isinstance(data, dict) else data}")
    return data, intent_token


def create_bot_platega_transaction(plan_months: int = 0, amount_cents: int | None = None, description: str | None = None) -> tuple[dict[str, object], str]:
    amount, currency, payment_method = platega_quote_amount((amount_cents / 100) if amount_cents is not None else bot_plan_price_usd(plan_months))
    intent_token = secrets.token_urlsafe(24)
    merchant_id, secret = platega_credentials()
    headers = {"X-MerchantId": merchant_id, "X-Secret": secret, "Accept": "application/json", "Content-Type": "application/json"}
    body = {"paymentMethod": payment_method, "paymentDetails": {"amount": amount, "currency": currency}, "description": description or f"VEXND subscription for {plan_months} month(s)", "payload": intent_token}
    from app.bot.common import telegram_bot_url

    tg_return = telegram_bot_url()
    if tg_return:
        body["return"] = tg_return
        body["failedUrl"] = tg_return
    resp = HTTP.post(f"{platega_api_base()}/v2/transaction/process", json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json() or {}
    if not isinstance(data, dict) or not (data.get("url") or data.get("redirect") or data.get("payformSuccessUrl")):
        raise RuntimeError("Platega create transaction response missing redirect URL")
    return data, intent_token


def create_bot_heleket_invoice(plan_months: int = 0, amount_cents: int | None = None, description: str | None = None, include_callback: bool = True) -> tuple[dict[str, object], str]:
    merchant_id, api_key = heleket_credentials()
    intent_token = secrets.token_urlsafe(24)
    order_id = secrets.token_hex(16)
    amount_usd = (amount_cents / 100) if amount_cents is not None else bot_plan_price_usd(plan_months)
    payload = {"amount": f"{amount_usd:g}", "currency": "USD", "order_id": order_id, "additional_data": intent_token}
    if include_callback:
        wh_secret = get_webhook_secret("HELEKET_WEBHOOK_PATH_SECRET")
        payload["url_callback"] = bot_webhook_url("heleket_webhook_secret", secret=wh_secret) if wh_secret else bot_webhook_url("heleket_webhook")
    headers = {"merchant": merchant_id, "sign": heleket_sign_payload(payload, api_key), "Content-Type": "application/json", "Accept": "application/json"}
    body = heleket_json_dumps(payload)
    resp = HTTP.post("https://api.heleket.com/v1/payment", data=body.encode("utf-8"), headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json() or {}
    invoice = data.get("result") if isinstance(data, dict) and isinstance(data.get("result"), dict) else data
    if not isinstance(invoice, dict):
        raise RuntimeError("Unexpected Heleket response")
    return invoice, intent_token


def create_bot_payment(user: User, method: str, plan_months: int) -> tuple[str, str, int]:
    amount_usd = bot_plan_price_usd(plan_months)
    if method == "cryptobot":
        invoice, intent_token = create_bot_crypto_invoice(plan_months)
        external_id = str(invoice.get("invoice_id") or invoice.get("invoiceId") or invoice.get("id") or "").strip()
        pay_url = str(invoice.get("bot_invoice_url") or invoice.get("pay_url") or "").strip()
    elif method == "crystal":
        invoice, intent_token = create_bot_crystal_invoice(user, plan_months)
        external_id = str(invoice.get("id") or "").strip()
        pay_url = str(invoice.get("url") or "").strip()
    elif method == "platega":
        invoice, intent_token = create_bot_platega_transaction(plan_months)
        external_id = str(invoice.get("transactionId") or invoice.get("transaction_id") or invoice.get("id") or "").strip()
        pay_url = str(invoice.get("url") or invoice.get("redirect") or invoice.get("payformSuccessUrl") or "").strip()
    elif method == "heleket":
        invoice, intent_token = create_bot_heleket_invoice(plan_months)
        external_id = str(invoice.get("uuid") or invoice.get("order_id") or invoice.get("id") or "").strip()
        pay_url = str(invoice.get("url") or "").strip()
    else:
        raise RuntimeError("Unknown payment method")
    if not external_id or not pay_url:
        raise RuntimeError("Provider response missing payment data")
    intent = PaymentIntent(provider=PAYMENT_METHODS[method]["provider"], token=intent_token, user_id=user.id, plan_months=plan_months, external_id=external_id or None)
    db.session.add(intent)
    db.session.add(create_bot_intent_pricing(intent_token, amount_usd))
    db.session.commit()
    return pay_url, PAYMENT_METHODS[method]["label"], intent.id


def create_balance_topup(user: User, state: BotUserState, method: str, amount_cents: int) -> tuple[str, str, int]:
    from app.bot.common import is_valid_topup_amount

    if method not in BALANCE_PROVIDERS or not is_valid_topup_amount(amount_cents):
        raise RuntimeError("Invalid top-up request")
    description = f"VEXND balance top-up {money(amount_cents)}"
    if method == "cryptobot":
        invoice, intent_token = create_bot_crypto_invoice(amount_cents=amount_cents, description=description)
        external_id = str(invoice.get("invoice_id") or invoice.get("invoiceId") or invoice.get("id") or "").strip()
        pay_url = str(invoice.get("bot_invoice_url") or invoice.get("pay_url") or "").strip()
    elif method == "crystal":
        invoice, intent_token = create_bot_crystal_invoice(user, amount_cents=amount_cents, description=description, include_callback=False)
        external_id = str(invoice.get("id") or "").strip()
        pay_url = str(invoice.get("url") or "").strip()
    elif method == "platega":
        invoice, intent_token = create_bot_platega_transaction(amount_cents=amount_cents, description=description)
        external_id = str(invoice.get("transactionId") or invoice.get("transaction_id") or invoice.get("id") or "").strip()
        pay_url = str(invoice.get("url") or invoice.get("redirect") or invoice.get("payformSuccessUrl") or "").strip()
    elif method == "heleket":
        invoice, intent_token = create_bot_heleket_invoice(amount_cents=amount_cents, description=description, include_callback=False)
        external_id = str(invoice.get("uuid") or invoice.get("order_id") or invoice.get("id") or "").strip()
        pay_url = str(invoice.get("url") or "").strip()
    else:
        raise RuntimeError("Unknown top-up method")
    if not external_id or not pay_url:
        raise RuntimeError("Provider response missing payment data")
    intent = PaymentIntent(provider=BALANCE_PROVIDERS[method], token=intent_token, user_id=user.id, plan_months=0, external_id=external_id or None)
    db.session.add(intent)
    db.session.flush()
    db.session.add(create_bot_intent_pricing(intent_token, Decimal(amount_cents) / Decimal("100")))
    db.session.add(BotBalanceTopup(telegram_id=state.telegram_id, user_id=user.id, payment_intent_id=intent.id, amount_cents=amount_cents))
    db.session.commit()
    return pay_url, PAYMENT_METHODS[method]["label"], intent.id


def handle_payment_method(chat_id: int, message_id: int, user: User, state: BotUserState, data: str) -> None:
    try:
        _prefix, method, months_raw = data.split("_", 2)
        plan_months = int(months_raw)
    except (ValueError, TypeError):
        edit_message(chat_id, message_id, t(state, "invoice_missing"), plans_keyboard(state, user))
        return
    from app.bot.keyboards import is_payment_method_enabled

    if method not in PAYMENT_METHODS or plan_months not in BOT_PLAN_CATALOG:
        edit_message(chat_id, message_id, "⚠️ Payment method unavailable. Choose another option:" if state.lang == "en" else "⚠️ Способ оплаты недоступен. Выберите другой вариант:", plans_keyboard(state, user))
        return
    if not is_payment_method_enabled(method):
        edit_message(chat_id, message_id, "🛠 This payment method is not configured yet. Choose another one:" if state.lang == "en" else "🛠 Этот способ оплаты пока не настроен. Выберите другой:", payment_methods_keyboard(plan_months, state))
        return
    try:
        pay_url, label, intent_id = create_bot_payment(user, method, plan_months)
    except Exception as exc:
        db.session.rollback()
        print(f"Bot payment creation failed: {exc}")
        edit_message(chat_id, message_id, t(state, "invoice_error"), payment_methods_keyboard(plan_months, state))
        return
    edit_message(chat_id, message_id, f"{t(state, 'invoice_created')}\n\n{t(state, 'method')}: <b>{h(label)}</b>\n📦 {t(state, 'plan')}: <b>{h(bot_plan_label(plan_months, state))}</b>\n💰 {t(state, 'amount')}: <b>{money_amount(bot_plan_price_usd(plan_months))}</b>\n\n{t(state, 'after_pay')}", payment_link_keyboard(pay_url, intent_id, plan_months, state))


def handle_balance_purchase(chat_id: int, message_id: int, user: User, state: BotUserState, data: str) -> None:
    try:
        plan_months = int(data.removeprefix("balance_buy_"))
    except ValueError:
        edit_message(chat_id, message_id, t(state, "invoice_missing"), profile_keyboard(state))
        return
    if plan_months not in BOT_PLAN_CATALOG:
        edit_message(chat_id, message_id, t(state, "invoice_missing"), profile_keyboard(state))
        return
    price_cents = int(bot_plan_price_usd(plan_months) * 100)
    if state.balance_cents < price_cents:
        edit_message(chat_id, message_id, t(state, "balance_not_enough"), subscription_keyboard(state))
        return
    try:
        create_remnawave_subscription(user, plan_months, strict=True)
        apply_referral_bonus_if_eligible(user)
        invalidate_remnawave_snapshot(user.id)
        state.balance_cents -= price_cents
        state.updated_at = utc_now()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print(f"Balance purchase provisioning failed: {exc}")
        edit_message(chat_id, message_id, t(state, "invoice_error"), subscription_keyboard(state))
        return
    text_out, _ = format_subscription(user, state, force_refresh=True)
    edit_message(chat_id, message_id, f"{t(state, 'paid_from_balance')}\n\n{text_out}", subscription_keyboard(state))


def handle_topup_payment_method(chat_id: int, message_id: int, user: User, state: BotUserState, data: str) -> None:
    try:
        _topup, _pm, method, amount_raw = data.split("_", 3)
        amount_cents = int(amount_raw)
    except (ValueError, TypeError):
        edit_message(chat_id, message_id, t(state, "invoice_missing"), profile_keyboard(state))
        return
    from app.bot.common import is_valid_topup_amount
    from app.bot.keyboards import is_payment_method_enabled

    if method not in BALANCE_PROVIDERS or not is_payment_method_enabled(method) or not is_valid_topup_amount(amount_cents):
        from app.bot.common import topup_invalid_amount_text

        edit_message(chat_id, message_id, topup_invalid_amount_text(state), topup_amounts_keyboard(state))
        return
    try:
        pay_url, label, intent_id = create_balance_topup(user, state, method, amount_cents)
    except Exception as exc:
        db.session.rollback()
        print(f"Balance top-up creation failed: {exc}")
        edit_message(chat_id, message_id, t(state, "invoice_error"), topup_payment_methods_keyboard(amount_cents, state))
        return
    edit_message(chat_id, message_id, f"{t(state, 'invoice_created')}\n\n{t(state, 'method')}: <b>{h(label)}</b>\n💰 {t(state, 'balance')}: <b>{money(amount_cents)}</b>\n\n{t(state, 'after_pay')}", {"inline_keyboard": [[{"text": t(state, "pay"), "url": pay_url}], [{"text": t(state, "check_payment"), "callback_data": f"checktopup_{intent_id}"}], [{"text": t(state, "back_menu"), "callback_data": "menu"}]]})


def verify_topup_payment(intent: PaymentIntent, topup: BotBalanceTopup) -> tuple[bool, str]:
    ensure_bot_intent_pricing(intent, topup)
    external_id = (intent.external_id or "").strip()
    amount_usd = topup.amount_cents / 100
    if not external_id:
        return False, "missing invoice id"
    if intent.provider == "botbal_crypto":
        inv = cryptobot_get_invoice_by_id(external_id)
        if not inv or str(inv.get("status") or "").strip() != "paid":
            return False, "not paid"
        if str(inv.get("payload") or "").strip() != str(intent.token or "").strip():
            return False, "token mismatch"
        if str(inv.get("fiat") or "").strip() and str(inv.get("fiat")).strip() != "USD":
            return False, "currency mismatch"
        if abs(float(inv.get("amount") or 0) - amount_usd) > 1e-9:
            return False, "amount mismatch"
    elif intent.provider == "botbal_crystal":
        info = crystal_invoice_info(external_id)
        if str(info.get("state") or "").strip() != "payed":
            return False, "not paid"
        if str(info.get("extra") or "").strip() != str(intent.token or "").strip():
            return False, "token mismatch"
        if str(info.get("currency") or "").strip() and str(info.get("currency")).strip().upper() != "USD":
            return False, "currency mismatch"
        if abs(float(info.get("amount") or 0) - amount_usd) > 1e-9:
            return False, "amount mismatch"
    elif intent.provider == "botbal_platega":
        tx = platega_get_transaction(external_id)
        if not tx or str(tx.get("status") or tx.get("Status") or "").strip().upper() != "CONFIRMED":
            return False, "not confirmed"
        if str(tx.get("payload") or "").strip() and str(tx.get("payload")).strip() != str(intent.token or "").strip():
            return False, "token mismatch"
        details = tx.get("paymentDetails") or {}
        expected_amount, expected_currency, _ = platega_quote_amount(amount_usd)
        currency = str(details.get("currency") or details.get("Currency") or "").strip().upper()
        if currency and currency != expected_currency:
            return False, "currency mismatch"
        try:
            paid_amount = float(details.get("amount") or details.get("Amount") or 0)
        except Exception:
            paid_amount = None
        if paid_amount is not None and abs(paid_amount - expected_amount) > 0.01:
            return False, "amount mismatch"
    elif intent.provider == "botbal_heleket":
        inv = heleket_payment_info(uuid=external_id) if "-" in external_id else heleket_payment_info(order_id=external_id)
        if not inv or str(inv.get("status") or inv.get("payment_status") or "").strip().lower() not in ("paid", "paid_over"):
            return False, "not paid"
        if str(inv.get("additional_data") or "").strip() != str(intent.token or "").strip():
            return False, "token mismatch"
        if str(inv.get("currency") or "").strip() and str(inv.get("currency")).strip().upper() != "USD":
            return False, "currency mismatch"
        try:
            paid_amount = float(inv.get("amount") or 0)
        except Exception:
            paid_amount = 0
        if abs(paid_amount - amount_usd) > 1e-9:
            return False, "amount mismatch"
    else:
        return False, "unknown provider"
    return True, "ok"


def handle_topup_check(chat_id: int, message_id: int, user: User, state: BotUserState, data: str) -> None:
    try:
        intent_id = int(data.removeprefix("checktopup_"))
    except ValueError:
        edit_message(chat_id, message_id, t(state, "invoice_missing"), profile_keyboard(state))
        return
    intent = db.session.get(PaymentIntent, intent_id)
    topup = BotBalanceTopup.query.filter_by(payment_intent_id=intent_id, user_id=user.id).first()
    if not intent or not topup:
        edit_message(chat_id, message_id, t(state, "invoice_missing"), profile_keyboard(state))
        return
    if topup.status == "paid":
        edit_message(chat_id, message_id, format_subscription(user, state)[0], profile_keyboard(state))
        return
    try:
        processed, _msg = verify_topup_payment(intent, topup)
    except Exception as exc:
        db.session.rollback()
        print(f"Balance top-up check failed: {exc}")
        processed = False
    if not processed:
        edit_message(chat_id, message_id, t(state, "payment_pending"), keyboard([[(t(state, "check_payment"), f"checktopup_{intent.id}")], [(t(state, "back_menu"), "menu")]]))
        return
    external_id = (intent.external_id or "").strip()
    if not external_id:
        edit_message(chat_id, message_id, t(state, "invoice_missing"), profile_keyboard(state))
        return
    with payment_processing_lock(intent.provider, external_id):
        db.session.refresh(intent)
        db.session.refresh(topup)
        db.session.refresh(state)
        if topup.status == "paid" or ProcessedPayment.query.filter_by(provider=intent.provider, external_id=external_id).first():
            edit_message(chat_id, message_id, format_subscription(user, state)[0], profile_keyboard(state))
            return
        db.session.add(ProcessedPayment(provider=intent.provider, external_id=external_id))
        intent.processed_at = utc_now()
        topup.status = "paid"
        topup.processed_at = utc_now()
        state.balance_cents += topup.amount_cents
        state.updated_at = utc_now()
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            edit_message(chat_id, message_id, format_subscription(user, state)[0], profile_keyboard(state))
            return
    edit_message(chat_id, message_id, t(state, "balance_payment_ok", amount=money(topup.amount_cents)), profile_keyboard(state))

def ensure_bot_schema() -> None:
    db.create_all()


def process_crystal_payment(intent: PaymentIntent) -> tuple[bool, str]:
    inv_id = (intent.external_id or "").strip()
    if not inv_id:
        return False, "missing invoice id"
    return crystalpay_process_paid_invoice(inv_id, intent.token)


def process_heleket_payment(intent: PaymentIntent) -> tuple[bool, str]:
    ext = (intent.external_id or "").strip()
    if not ext:
        return False, "missing invoice id"
    return heleket_process_paid(ext=ext, token=intent.token)


def process_payment_intent(intent: PaymentIntent) -> tuple[bool, str]:
    ensure_bot_intent_pricing(intent)
    if intent.processed_at:
        return True, "already processed"
    external_id = (intent.external_id or "").strip()
    if intent.provider == "cryptobot":
        return cryptobot_process_paid_invoice(external_id, intent.token)
    if intent.provider == "crystalpay":
        return process_crystal_payment(intent)
    if intent.provider == "platega":
        return platega_process_paid_transaction(external_id, intent.token)
    if intent.provider == "heleket":
        return process_heleket_payment(intent)
    return False, "unknown provider"


def handle_payment_check(chat_id: int, message_id: int, user: User, state: BotUserState, data: str) -> None:
    try:
        intent_id = int(data.removeprefix("checkpay_"))
    except ValueError:
        edit_message(chat_id, message_id, t(state, "invoice_missing"), main_menu(state, user))
        return
    intent = db.session.get(PaymentIntent, intent_id)
    if not intent or intent.user_id != user.id:
        edit_message(chat_id, message_id, t(state, "invoice_missing"), main_menu(state, user))
        return
    try:
        processed, msg = process_payment_intent(intent)
    except Exception as exc:
        db.session.rollback()
        print(f"Bot payment check failed: {exc}")
        processed, msg = False, "temporary error"
    if not processed:
        edit_message(chat_id, message_id, t(state, "payment_pending"), keyboard([[(t(state, "check_payment"), f"checkpay_{intent.id}")], [(t(state, "back_menu"), "menu")]]))
        return
    invalidate_remnawave_snapshot(user.id)
    text_out, _ = format_subscription(user, state, force_refresh=True)
    edit_message(chat_id, message_id, f"{t(state, 'payment_ok')}\n\n{t(state, 'sub_activated')}\n\n" + text_out, subscription_keyboard(state))
