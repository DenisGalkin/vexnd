from __future__ import annotations

import os
import secrets

from app.bot.common import (
    BOT_PLAN_CATALOG,
    PAYMENT_METHODS,
    HTTP,
    bot_plan_label,
    bot_plan_price_usd,
    bot_webhook_url,
    create_bot_intent_pricing,
    db,
    edit_photo_caption,
    edit_message,
    ensure_bot_intent_pricing,
    h,
    money_amount,
    replace_message_with_screen,
    t,
)
from app.bot.keyboards import balance_topup_methods_keyboard, keyboard, main_menu, payment_link_keyboard, payment_methods_keyboard_for_user, plans_keyboard, profile_keyboard
from app.bot.models import BotUserState
from app.bot.subscriptions import (
    invalidate_remnawave_snapshot,
    remnawave_subscription_snapshot,
    render_profile_text,
    render_subscription_text,
    subscription_markup,
)
from app.domain.models import PaymentIntent, User
from app.services.balance import MAX_TOPUP_CENTS, MIN_TOPUP_CENTS, amount_to_cents, can_pay_for_plan_with_balance, format_balance_cents, purchase_subscription_with_balance, topup_description, user_balance_cents
from app.services.payments.crystalpay import crystal_credentials, crystal_invoice_info, crystalpay_process_paid_invoice
from app.services.payments.cryptobot import cryptobot_api_base, cryptobot_get_invoice_by_id, cryptobot_process_paid_invoice
from app.services.payments.heleket import heleket_credentials, heleket_json_dumps, heleket_payment_info, heleket_process_paid, heleket_sign_payload
from app.services.payments.platega import platega_api_base, platega_credentials, platega_format_amount, platega_get_transaction, platega_process_paid_transaction, platega_quote_amount, platega_raise_for_status
from app.services.bot_admin_links import ensure_bot_admin_schema
from app.services.security import get_webhook_secret


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


def create_bot_platega_transaction(plan_months: int = 0, amount_cents: int | None = None, description: str | None = None) -> tuple[dict[str, object], str, dict[str, str]]:
    amount, currency, payment_method = platega_quote_amount((amount_cents / 100) if amount_cents is not None else bot_plan_price_usd(plan_months))
    intent_token = secrets.token_urlsafe(24)
    merchant_id, secret = platega_credentials()
    headers = {"X-MerchantId": merchant_id, "X-Secret": secret, "Accept": "application/json", "Content-Type": "application/json"}
    payment_details = {"amount": platega_format_amount(amount), "currency": currency}
    body = {
        "paymentMethod": payment_method,
        "paymentDetails": {"amount": float(payment_details["amount"]), "currency": payment_details["currency"]},
        "description": description or f"VEXND subscription for {plan_months} month(s)",
        "payload": intent_token,
    }
    from app.bot.common import telegram_bot_url

    tg_return = telegram_bot_url()
    if tg_return:
        body["return"] = tg_return
        body["failedUrl"] = tg_return
    resp = HTTP.post(f"{platega_api_base()}/transaction/process", json=body, headers=headers, timeout=30)
    platega_raise_for_status(resp)
    data = resp.json() or {}
    if not isinstance(data, dict) or not (data.get("url") or data.get("redirect") or data.get("payformSuccessUrl")):
        raise RuntimeError("Platega create transaction response missing redirect URL")
    return data, intent_token, payment_details


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


def create_bot_payment(
    user: User,
    method: str,
    plan_months: int,
    *,
    purpose: str = "subscription",
    amount_cents: int | None = None,
    description: str | None = None,
) -> tuple[str, str, int]:
    if purpose == "balance_topup":
        if amount_cents is None:
            raise RuntimeError("Balance top-up amount is required")
        amount_usd = amount_cents / 100
    else:
        amount_usd = bot_plan_price_usd(plan_months)
    if method == "cryptobot":
        invoice, intent_token = create_bot_crypto_invoice(plan_months, amount_cents=amount_cents, description=description)
        external_id = str(invoice.get("invoice_id") or invoice.get("invoiceId") or invoice.get("id") or "").strip()
        pay_url = str(invoice.get("bot_invoice_url") or invoice.get("pay_url") or "").strip()
    elif method == "crystal":
        invoice, intent_token = create_bot_crystal_invoice(user, plan_months, amount_cents=amount_cents, description=description)
        external_id = str(invoice.get("id") or "").strip()
        pay_url = str(invoice.get("url") or "").strip()
    elif method == "platega":
        invoice, intent_token, payment_details = create_bot_platega_transaction(plan_months, amount_cents=amount_cents, description=description)
        external_id = str(invoice.get("transactionId") or invoice.get("transaction_id") or invoice.get("id") or "").strip()
        pay_url = str(invoice.get("url") or invoice.get("redirect") or invoice.get("payformSuccessUrl") or "").strip()
    elif method == "heleket":
        invoice, intent_token = create_bot_heleket_invoice(plan_months, amount_cents=amount_cents, description=description)
        external_id = str(invoice.get("uuid") or invoice.get("order_id") or invoice.get("id") or "").strip()
        pay_url = str(invoice.get("url") or "").strip()
    else:
        raise RuntimeError("Unknown payment method")
    if not external_id or not pay_url:
        raise RuntimeError("Provider response missing payment data")
    intent = PaymentIntent(
        provider=PAYMENT_METHODS[method]["provider"],
        token=intent_token,
        user_id=user.id,
        plan_months=plan_months,
        purpose=purpose,
        balance_amount_cents=amount_cents if purpose == "balance_topup" else None,
        external_id=external_id or None,
        expected_provider_amount=(payment_details["amount"] if method == "platega" else f"{amount_usd:.2f}"),
        expected_provider_currency=(payment_details["currency"] if method == "platega" else "USD"),
    )
    db.session.add(intent)
    db.session.add(create_bot_intent_pricing(intent_token, amount_usd))
    db.session.commit()
    return pay_url, PAYMENT_METHODS[method]["label"], intent.id


def handle_payment_method(chat_id: int, message_id: int, user: User, state: BotUserState, data: str) -> None:
    try:
        _prefix, method, months_raw = data.split("_", 2)
        plan_months = int(months_raw)
    except (ValueError, TypeError):
        edit_photo_caption(chat_id, message_id, t(state, "invoice_missing"), plans_keyboard(state, user))
        return
    from app.bot.keyboards import is_payment_method_enabled

    if method not in PAYMENT_METHODS or plan_months not in BOT_PLAN_CATALOG:
        edit_photo_caption(chat_id, message_id, "⚠️ Payment method unavailable. Choose another option:" if state.lang == "en" else "⚠️ Способ оплаты недоступен. Выберите другой вариант:", plans_keyboard(state, user))
        return
    if not is_payment_method_enabled(method):
        edit_photo_caption(chat_id, message_id, "🛠 This payment method is not configured yet. Choose another one:" if state.lang == "en" else "🛠 Этот способ оплаты пока не настроен. Выберите другой:", payment_methods_keyboard_for_user(plan_months, state, user))
        return
    if method == "balance":
        shortfall_cents = balance_shortfall_cents(user, plan_months)
        if shortfall_cents > 0:
            edit_photo_caption(
                chat_id,
                message_id,
                render_balance_shortfall_text(state, user, plan_months),
                keyboard(
                    [
                        [(t(state, "balance_topup_shortfall", amount=format_balance_cents(shortfall_cents)), f"balance_shortfall_{plan_months}")],
                        [(t(state, "other_method"), f"buy_{plan_months}")],
                    ]
                ),
            )
            return
        try:
            purchase_subscription_with_balance(user, plan_months)
        except ValueError as exc:
            db.session.rollback()
            if str(exc) == "insufficient_balance":
                edit_photo_caption(chat_id, message_id, t(state, "balance_not_enough"), payment_methods_keyboard_for_user(plan_months, state, user))
            else:
                edit_photo_caption(chat_id, message_id, str(exc), payment_methods_keyboard_for_user(plan_months, state, user))
            return
        except Exception as exc:
            db.session.rollback()
            print(f"Bot balance purchase failed: {exc}")
            edit_photo_caption(chat_id, message_id, t(state, "invoice_error"), payment_methods_keyboard_for_user(plan_months, state, user))
            return
        invalidate_remnawave_snapshot(user.id)
        snapshot = remnawave_subscription_snapshot(user, force_refresh=True)
        text_out, _ = render_subscription_text(snapshot, state)
        replace_message_with_screen(chat_id, message_id, "subscription", f"{t(state, 'balance_pay_ok')}\n\n{text_out}", subscription_markup(snapshot, state))
        return
    try:
        pay_url, label, intent_id = create_bot_payment(user, method, plan_months)
    except Exception as exc:
        db.session.rollback()
        print(f"Bot payment creation failed: {exc}")
        edit_photo_caption(chat_id, message_id, t(state, "invoice_error"), payment_methods_keyboard_for_user(plan_months, state, user))
        return
    edit_photo_caption(chat_id, message_id, f"{t(state, 'invoice_created')}\n\n{t(state, 'method')}: <b>{h(label)}</b>\n📦 {t(state, 'plan')}: <b>{h(bot_plan_label(plan_months, state))}</b>\n💰 {t(state, 'amount')}: <b>{money_amount(bot_plan_price_usd(plan_months))}</b>\n\n{t(state, 'after_pay')}", payment_link_keyboard(pay_url, intent_id, plan_months, state))


def render_balance_text(state: BotUserState, user: User) -> str:
    return (
        f"{t(state, 'balance_title')}\n\n"
        f"💵 {t(state, 'balance_current')}: <b>{format_balance_cents(user_balance_cents(user.id))}</b>\n\n"
        f"{t(state, 'balance_topup_limits', min_amount=format_balance_cents(MIN_TOPUP_CENTS), max_amount=format_balance_cents(MAX_TOPUP_CENTS))}\n\n"
        f"{t(state, 'balance_topup_hint')}"
    )


def balance_shortfall_cents(user: User, plan_months: int) -> int:
    allowed, pricing = can_pay_for_plan_with_balance(user.id, plan_months)
    if allowed:
        return 0
    needed_cents = amount_to_cents(pricing["final_price"])
    current_cents = user_balance_cents(user.id)
    return max(0, needed_cents - current_cents)


def render_balance_shortfall_text(state: BotUserState, user: User, plan_months: int) -> str:
    shortfall_cents = balance_shortfall_cents(user, plan_months)
    return (
        f"{t(state, 'balance_not_enough')}\n\n"
        f"💵 {t(state, 'balance_current')}: <b>{format_balance_cents(user_balance_cents(user.id))}</b>\n"
        f"💳 {t(state, 'balance_shortfall')}: <b>{format_balance_cents(shortfall_cents)}</b>\n\n"
        f"{t(state, 'balance_shortfall_hint')}"
    )


def handle_balance_topup_method(chat_id: int, message_id: int, user: User, state: BotUserState, data: str) -> None:
    try:
        raw = data.removeprefix("balance_pm_")
        method, amount_raw = raw.rsplit("_", 1)
        amount_cents = int(amount_raw)
    except Exception:
        edit_photo_caption(chat_id, message_id, t(state, "invoice_error"), profile_keyboard(state))
        return
    try:
        pay_url, label, intent_id = create_bot_payment(
            user,
            method,
            0,
            purpose="balance_topup",
            amount_cents=amount_cents,
            description=topup_description(amount_cents),
        )
    except Exception as exc:
        db.session.rollback()
        print(f"Bot balance top-up creation failed: {exc}")
        edit_photo_caption(chat_id, message_id, t(state, "invoice_error"), balance_topup_methods_keyboard(amount_cents, state))
        return
    edit_photo_caption(
        chat_id,
        message_id,
        f"{t(state, 'invoice_created')}\n\n{t(state, 'method')}: <b>{h(label)}</b>\n💰 {t(state, 'amount')}: <b>{format_balance_cents(amount_cents)}</b>\n\n{t(state, 'after_pay')}",
        payment_link_keyboard(pay_url, intent_id, 0, state, return_callback="balance_topup"),
    )

def ensure_bot_schema() -> None:
    db.create_all()
    ensure_bot_admin_schema()


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
        edit_photo_caption(chat_id, message_id, t(state, "invoice_missing"), main_menu(state, user))
        return
    intent = db.session.get(PaymentIntent, intent_id)
    if not intent or intent.user_id != user.id:
        edit_photo_caption(chat_id, message_id, t(state, "invoice_missing"), main_menu(state, user))
        return
    try:
        processed, msg = process_payment_intent(intent)
    except Exception as exc:
        db.session.rollback()
        print(f"Bot payment check failed: {exc}")
        processed, msg = False, "temporary error"
    if not processed:
        edit_photo_caption(chat_id, message_id, t(state, "payment_pending"), keyboard([[(t(state, "check_payment"), f"checkpay_{intent.id}")], [(t(state, "back_menu"), "menu")]]))
        return
    if (getattr(intent, "purpose", "subscription") or "subscription") == "balance_topup":
        replace_message_with_screen(chat_id, message_id, "profile", f"{t(state, 'balance_topup_ok')}\n\n{render_profile_text(user, state)}", profile_keyboard(state))
        return
    invalidate_remnawave_snapshot(user.id)
    snapshot = remnawave_subscription_snapshot(user, force_refresh=True)
    text_out, _ = render_subscription_text(snapshot, state)
    replace_message_with_screen(chat_id, message_id, "subscription", f"{t(state, 'payment_ok')}\n\n{t(state, 'sub_activated')}\n\n" + text_out, subscription_markup(snapshot, state))
