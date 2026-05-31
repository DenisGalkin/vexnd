from __future__ import annotations

import html
import io
import json
import os
import secrets
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import quote

import qrcode
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from werkzeug.local import LocalProxy

from app.core.config import SITE_ORIGIN
from app.core.extensions import db
from app.domain.models import PaymentIntentPricing, User
from app.services.payment_intents import create_intent_with_pricing
from app.services.remnawave import (
    is_telegram_placeholder_email,
    rw_username_from_email,
    rw_username_from_telegram,
    telegram_local_placeholder_email,
)
from app.bot.content import BOT_PLAN_CATALOG, CONNECT_CLIENTS, PAYMENT_METHODS, TEXTS
from app.bot.models import BotUserState, TelegramAccount, utc_now


load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env"))

_bot_app = None


def get_app():
    global _bot_app
    if _bot_app is None:
        from app import create_app

        _bot_app = create_app()
    return _bot_app


app = LocalProxy(get_app)

BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
SUPPORT_USERNAME = (os.environ.get("TELEGRAM_SUPPORT_USERNAME") or "vexndsupport").strip().lstrip("@")
BOT_USERNAME = (os.environ.get("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
SUBSCRIPTION_REMINDER_CHECK_INTERVAL_SECONDS = int(os.environ.get("BOT_SUBSCRIPTION_REMINDER_CHECK_INTERVAL_SECONDS", "600"))
BOT_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets", "screens")
BOT_SCREEN_IMAGES = {
    "menu": os.path.join(BOT_ASSETS_DIR, "main.png"),
    "connect": os.path.join(BOT_ASSETS_DIR, "connection.png"),
    "subscription": os.path.join(BOT_ASSETS_DIR, "subscription.png"),
    "payment": os.path.join(BOT_ASSETS_DIR, "payment.png"),
    "referrals": os.path.join(BOT_ASSETS_DIR, "referrals.png"),
    "profile": os.path.join(BOT_ASSETS_DIR, "profile.png"),
}


def build_http_session() -> requests.Session:
    """
    Create an HTTP session for bot API requests with connection pooling and optional
    retry support. Pool sizes and retry counts can be tuned via environment
    variables. Allowing limited retries helps absorb transient network errors
    without surfacing them to end users. The defaults mirror those used by
    the main application HTTP client defined in ``app.core.config``.

    Environment variables:
      HTTP_POOL_CONNECTIONS: number of connection pools to maintain (default 20)
      HTTP_POOL_MAXSIZE: maximum number of connections per pool (default 50)
      HTTP_MAX_RETRIES: number of automatic retries on transient errors (default 3)
    """
    session = requests.Session()
    pool_connections = int(os.environ.get("HTTP_POOL_CONNECTIONS", "20"))
    pool_maxsize = int(os.environ.get("HTTP_POOL_MAXSIZE", "50"))
    # Permit a few automatic retries on transient failures. If the env var is unset or
    # invalid, fall back to three retries – similar to the web client.
    try:
        max_retries = int(os.environ.get("HTTP_MAX_RETRIES", "3"))
    except Exception:
        max_retries = 3
    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=max_retries,
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


def ensure_bot_intent_pricing(intent) -> None:
    if not intent or not intent.token:
        return
    if PaymentIntentPricing.query.filter_by(intent_token=intent.token).first():
        return
    amount_usd: Decimal | None = None
    if intent.provider in {"cryptobot", "crystalpay", "platega", "heleket"} and intent.plan_months in BOT_PLAN_CATALOG:
        amount_usd = bot_plan_price_usd(intent.plan_months)
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


def send_message(chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api("sendMessage", payload)


def ensure_telegram_user_password(chat_id: int, user: User, state: BotUserState | None) -> str | None:
    if not user or (user.password_hash or "").strip():
        return None
    password = secrets.token_urlsafe(12)
    user.set_password(password)
    db.session.commit()
    send_message(chat_id, t(state, "telegram_generated_password", password=h(password)))
    return password


def send_photo(chat_id: int, png_bytes: bytes, caption: str = "", reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    files = {"photo": ("subscription-qr.png", png_bytes, "image/png")}
    data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    resp = HTTP.post(f"{BOT_API}/sendPhoto", data=data, files=files, timeout=30)
    resp.raise_for_status()
    return resp.json()


def read_screen_image(screen: str) -> bytes:
    path = BOT_SCREEN_IMAGES.get(screen)
    if not path:
        raise KeyError(f"Unknown bot screen image: {screen}")
    with open(path, "rb") as file_obj:
        return file_obj.read()


def send_screen(chat_id: int, screen: str, caption: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    return send_photo(chat_id, read_screen_image(screen), caption, reply_markup)


def edit_photo_caption(chat_id: int, message_id: int, caption: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any] | None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        return api("editMessageCaption", payload)
    except Exception as exc:
        msg = str(exc).lower()
        if "message is not modified" in msg:
            return None
        if "there is no caption in the message to edit" in msg or "message to edit not found" in msg or "message can't be edited" in msg:
            send_message(chat_id, caption, reply_markup)
            return None
        raise


def edit_screen_message(
    chat_id: int,
    message_id: int,
    screen: str,
    caption: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    media = {
        "type": "photo",
        "media": "attach://screen",
        "caption": caption,
        "parse_mode": "HTML",
    }
    data = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "media": json.dumps(media, ensure_ascii=False),
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"screen": (f"{screen}.png", read_screen_image(screen), "image/png")}
    resp = HTTP.post(f"{BOT_API}/editMessageMedia", data=data, files=files, timeout=30)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if resp.status_code < 400 and isinstance(payload, dict) and payload.get("ok"):
        return payload
    error_text = str(payload or resp.text).lower()
    if (
        "message content is not modified" in error_text
        or "message is not modified" in error_text
        or "there is no media in the message to edit" in error_text
        or "message can't be edited" in error_text
        or "message to edit not found" in error_text
        or "type of message content cannot be edited" in error_text
    ):
        return send_screen(chat_id, screen, caption, reply_markup)
    raise RuntimeError(f"Telegram API error ({resp.status_code}): {payload or resp.text}")


def replace_message_with_screen(
    chat_id: int,
    message_id: int,
    screen: str,
    caption: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return edit_screen_message(chat_id, message_id, screen, caption, reply_markup)


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
    caption_payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": t(state, "loading"),
        "parse_mode": "HTML",
    }
    try:
        api("editMessageCaption", caption_payload)
        return
    except Exception:
        try:
            api(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": t(state, "loading"),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
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


def get_or_create_account(tg_user: dict[str, Any]) -> tuple[TelegramAccount, User, bool]:
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
        return account, user, False
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
    return account, user, True


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
