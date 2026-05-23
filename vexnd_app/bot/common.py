from __future__ import annotations

import html
import io
import json
import os
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import quote

import qrcode
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

from vexnd_app import create_app
from vexnd_app.config import SITE_ORIGIN
from vexnd_app.extensions import db
from vexnd_app.models import PaymentIntentPricing, User
from vexnd_app.services.payment_intents import create_intent_with_pricing
from vexnd_app.services.remnawave import (
    is_telegram_placeholder_email,
    rw_username_from_email,
    rw_username_from_telegram,
    telegram_local_placeholder_email,
)
from vexnd_bot.content import BALANCE_PROVIDERS, BOT_PLAN_CATALOG, CONNECT_CLIENTS, PAYMENT_METHODS, TEXTS
from vexnd_bot.models import BotBalanceTopup, BotPromoCode, BotPromoRedemption, BotUserState, TelegramAccount, utc_now


load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = create_app()

BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
SUPPORT_USERNAME = (os.environ.get("TELEGRAM_SUPPORT_USERNAME") or "vexndsupport").strip().lstrip("@")
BOT_USERNAME = (os.environ.get("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
MIN_TOPUP_CENTS = int(os.environ.get("BOT_MIN_TOPUP_CENTS", "100"))
MAX_TOPUP_CENTS = int(os.environ.get("BOT_MAX_TOPUP_CENTS", "50000"))
SUBSCRIPTION_REMINDER_CHECK_INTERVAL_SECONDS = int(os.environ.get("BOT_SUBSCRIPTION_REMINDER_CHECK_INTERVAL_SECONDS", "600"))


def build_http_session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=int(os.environ.get("HTTP_POOL_CONNECTIONS", "20")),
        pool_maxsize=int(os.environ.get("HTTP_POOL_MAXSIZE", "50")),
        max_retries=0,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


HTTP = build_http_session()


def h(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def site_url(path: str) -> str:
    return SITE_ORIGIN.rstrip("/") + "/" + path.lstrip("/")


def bot_webhook_url(endpoint: str, **values: str) -> str:
    if endpoint == "crystalpay_webhook_secret":
        return site_url(f"/crystalpay/webhook/{quote(str(values['secret']), safe='')}")
    if endpoint == "crystalpay_webhook":
        return site_url("/crystalpay/webhook")
    if endpoint == "heleket_webhook_secret":
        return site_url(f"/heleket/webhook/{quote(str(values['secret']), safe='')}")
    if endpoint == "heleket_webhook":
        return site_url("/heleket/webhook")
    raise ValueError(f"Unknown webhook endpoint: {endpoint}")


def api(method: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    resp = HTTP.post(f"{BOT_API}/{method}", json=payload, timeout=30, **kwargs)
    try:
        data = resp.json()
    except Exception:
        data = None
    if resp.status_code >= 400:
        raise RuntimeError(f"Telegram API error ({resp.status_code}): {data or resp.text}")
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def telegram_bot_url() -> str | None:
    global BOT_USERNAME
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}"
    try:
        data = api("getMe", {})
        username = ((data.get("result") or {}).get("username") or "").strip()
        if username:
            BOT_USERNAME = username
            return f"https://t.me/{BOT_USERNAME}"
    except Exception as exc:
        print(f"Telegram getMe failed: {exc}")
    return None


def t(state: BotUserState | None, key: str, **kwargs: Any) -> str:
    lang = state.lang if state and state.lang in TEXTS else "ru"
    value = TEXTS.get(lang, TEXTS["ru"]).get(key, TEXTS["ru"].get(key, key))
    return value.format(**kwargs) if kwargs else value


def bot_plan_price_usd(plan_months: int) -> Decimal:
    plan = BOT_PLAN_CATALOG.get(int(plan_months))
    if not plan:
        raise KeyError(f"Unsupported bot plan: {plan_months}")
    return Decimal(plan["price"])


def bot_plan_label(plan_months: int, state: BotUserState) -> str:
    plan = BOT_PLAN_CATALOG.get(int(plan_months))
    if not plan:
        return str(plan_months)
    return plan["label_en"] if state.lang == "en" else plan["label_ru"]


def create_bot_intent_pricing(token: str, amount_usd: Decimal | float | int | str) -> PaymentIntentPricing:
    amount_text = f"{Decimal(str(amount_usd)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
    return PaymentIntentPricing(
        intent_token=token,
        coupon_code=None,
        original_amount_usd=amount_text,
        final_amount_usd=amount_text,
        discount_amount_usd="0.00",
    )


def ensure_bot_intent_pricing(intent, topup: BotBalanceTopup | None = None) -> None:
    if not intent or not intent.token:
        return
    if PaymentIntentPricing.query.filter_by(intent_token=intent.token).first():
        return
    amount_usd: Decimal | None = None
    if intent.provider in {"cryptobot", "crystalpay", "platega", "heleket"} and intent.plan_months in BOT_PLAN_CATALOG:
        amount_usd = bot_plan_price_usd(intent.plan_months)
    elif topup is not None:
        amount_usd = Decimal(topup.amount_cents) / Decimal("100")
    if amount_usd is None:
        return
    db.session.add(create_bot_intent_pricing(intent.token, amount_usd))
    db.session.commit()


def money(cents: int) -> str:
    return f"${max(0, int(cents)) / 100:.2f}"


def money_amount(amount: Decimal | float | int | str) -> str:
    return f"${Decimal(str(amount)).quantize(Decimal('0.01')):.2f}"


def format_bytes(num_bytes: int | None) -> str:
    if num_bytes is None or num_bytes < 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(num_bytes)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    precision = 0 if value >= 100 else 1 if value >= 10 else 2
    return f"{value:.{precision}f} {units[unit_index]}"


def find_remote_value(obj: Any, names: set[str]) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in names:
                return value
        for value in obj.values():
            found = find_remote_value(value, names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_remote_value(item, names)
            if found is not None:
                return found
    return None


def coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def parse_topup_amount_cents(text: str) -> int | None:
    normalized = text.strip().replace(" ", "").replace("$", "").replace(",", ".")
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    cents = int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if not is_valid_topup_amount(cents):
        return None
    return cents


def is_valid_topup_amount(amount_cents: int) -> bool:
    return MIN_TOPUP_CENTS <= int(amount_cents) <= MAX_TOPUP_CENTS


def topup_invalid_amount_text(state: BotUserState) -> str:
    return t(state, "topup_invalid_amount", min_amount=f"{MIN_TOPUP_CENTS / 100:g}", max_amount=f"{MAX_TOPUP_CENTS / 100:g}")


def topup_amount_selected_text(state: BotUserState, amount_cents: int) -> str:
    return t(state, "topup_amount_selected", amount=money(amount_cents), choose_payment=t(state, "choose_payment"))


def clear_pending_action(state: BotUserState) -> None:
    if state.pending_action:
        state.pending_action = None
        state.updated_at = utc_now()
        db.session.commit()


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})
    except Exception as exc:
        print(f"answerCallbackQuery failed: {exc}")


def send_message(chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    api("sendMessage", payload)


def send_photo(chat_id: int, png_bytes: bytes, caption: str = "", reply_markup: dict[str, Any] | None = None) -> None:
    files = {"photo": ("subscription-qr.png", png_bytes, "image/png")}
    data = {"chat_id": str(chat_id), "caption": caption}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    resp = HTTP.post(f"{BOT_API}/sendPhoto", data=data, files=files, timeout=30)
    resp.raise_for_status()


def edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        api("editMessageText", payload)
    except Exception as exc:
        msg = str(exc).lower()
        if "message is not modified" in msg:
            return
        if "there is no text in the message to edit" in msg or "message can't be edited" in msg or "message to edit not found" in msg:
            send_message(chat_id, text, reply_markup)
            return
        raise


def show_loading(chat_id: int, message_id: int, state: BotUserState) -> None:
    try:
        edit_message(chat_id, message_id, t(state, "loading"))
    except Exception:
        pass


def get_or_create_state(tg_user: dict[str, Any]) -> BotUserState:
    telegram_id = int(tg_user["id"])
    state = BotUserState.query.filter_by(telegram_id=telegram_id).first()
    if state:
        return state
    state = BotUserState(telegram_id=telegram_id, lang="ru", pending_action="choose_language")
    db.session.add(state)
    db.session.commit()
    return state


def get_or_create_account(tg_user: dict[str, Any]) -> tuple[TelegramAccount, User]:
    telegram_id = int(tg_user["id"])
    username = tg_user.get("username")
    local_placeholder_email = telegram_local_placeholder_email(telegram_id)
    account = TelegramAccount.query.filter_by(telegram_id=telegram_id).first()
    if account:
        user = db.session.get(User, account.user_id)
        changed = False
        if not user:
            user = User(email=local_placeholder_email, lang="ru")
            user.set_password(secrets.token_urlsafe(32))
            db.session.add(user)
            db.session.flush()
            account.user_id = user.id
            changed = True
        elif is_telegram_placeholder_email(user.email) and user.email != local_placeholder_email:
            existing = User.query.filter_by(email=local_placeholder_email).first()
            if not existing or existing.id == user.id:
                user.email = local_placeholder_email
                changed = True
        first_name = tg_user.get("first_name")
        last_name = tg_user.get("last_name")
        if account.username != username:
            account.username = username
            changed = True
        if account.first_name != first_name:
            account.first_name = first_name
            changed = True
        if account.last_name != last_name:
            account.last_name = last_name
            changed = True
        if changed:
            account.updated_at = utc_now()
            db.session.commit()
        return account, user
    email = local_placeholder_email
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email, lang="ru")
        user.set_password(secrets.token_urlsafe(32))
        db.session.add(user)
        db.session.flush()
    account = TelegramAccount(
        telegram_id=telegram_id,
        user_id=user.id,
        username=tg_user.get("username"),
        first_name=tg_user.get("first_name"),
        last_name=tg_user.get("last_name"),
    )
    db.session.add(account)
    db.session.commit()
    return account, user


def device_title(device_code: str, state: BotUserState) -> str:
    mapping = {
        "ios": t(state, "device_ios"),
        "android": t(state, "device_android"),
        "pc": t(state, "device_pc"),
        "windows": t(state, "device_windows"),
        "macos": t(state, "device_macos"),
    }
    return mapping.get(device_code, device_code)


def client_meta(device_code: str, client_code: str) -> dict[str, str] | None:
    return next((item for item in CONNECT_CLIENTS.get(device_code, []) if item["code"] == client_code), None)


def client_download_url(client: dict[str, str], state: BotUserState) -> str | None:
    if state.lang == "ru" and client.get("url_ru"):
        return client.get("url_ru")
    if client.get("url_global"):
        return client.get("url_global")
    return client.get("url")


def client_import_url(user: User, client_code: str, subscription_url: str) -> str:
    sub_url = (subscription_url or "").strip()
    if client_code == "happ":
        return f"happ://add/{sub_url}"
    if client_code == "v2raytun":
        return f"v2raytun://import/{sub_url}"
    if client_code == "v2rayng":
        account = TelegramAccount.query.filter_by(user_id=user.id).first()
        profile_name = rw_username_from_telegram(account.telegram_id if account else None, account.username if account else None)
        if not account:
            profile_name = rw_username_from_email(user.email)
        return "v2rayng://install-config" f"?name={quote(profile_name, safe='')}" f"&url={quote(sub_url, safe='')}"
    if client_code == "flclashx":
        return f"flclashx://install-config?url={quote(sub_url, safe='')}"
    return sub_url


def browser_import_url(user: User, client: dict[str, str], state: BotUserState, subscription_url: str) -> str:
    target = client_import_url(user, client["code"], subscription_url)
    fallback = client_download_url(client, state) or ""
    return site_url("/open-app") + f"?target={quote(target, safe='')}" + (f"&fallback={quote(fallback, safe='')}" if fallback else "")


def make_qr_png(text: str) -> bytes:
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def seed_promo_codes() -> None:
    raw = (os.environ.get("BOT_PROMO_CODES") or "").strip()
    if not raw:
        return
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        parts = [p.strip() for p in item.split(":")]
        if len(parts) < 2:
            continue
        code = parts[0].upper()
        if BotPromoCode.query.filter_by(code=code).first():
            continue
        value = parts[1].lower()
        max_uses = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        promo = BotPromoCode(code=code, max_uses=max_uses)
        if value.startswith("plan"):
            promo.plan_months = int(value.removeprefix("plan") or "1")
        else:
            promo.balance_cents = int(float(value) * 100)
        db.session.add(promo)
    db.session.commit()

