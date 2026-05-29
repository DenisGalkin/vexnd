from __future__ import annotations

import os
from urllib.parse import quote

from app.bot.common import (
    CONNECT_CLIENTS,
    PAYMENT_METHODS,
    SUPPORT_USERNAME,
    bot_plan_label,
    bot_plan_price_usd,
    client_download_url,
    device_title,
    h,
    money_amount,
    site_url,
    t,
)
from app.domain.models import User
from app.services.subscriptions import is_trial_eligible
from app.services.bot_admin_links import is_bot_admin
from app.bot.models import BotUserState, TelegramAccount


def keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, object]:
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}


def payment_link_keyboard(pay_url: str, intent_id: int, plan_months: int, state: BotUserState) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [{"text": t(state, "pay"), "url": pay_url}],
            [
                {"text": t(state, "check_payment"), "callback_data": f"checkpay_{intent_id}"},
                {"text": t(state, "other_method"), "callback_data": f"buy_{plan_months}"},
            ],
            [{"text": t(state, "back_menu"), "callback_data": "menu"}],
        ]
    }


def main_menu(state: BotUserState, user: User | None = None) -> dict[str, object]:
    rows = [[{"text": t(state, "setup"), "callback_data": "connect"}, {"text": t(state, "profile"), "callback_data": "subscription"}]]
    if user is not None and is_trial_eligible(user):
        rows.append([{"text": t(state, "trial_offer"), "callback_data": "trial_activate"}])
    rows.append([{"text": t(state, "buy"), "callback_data": "plans"}, {"text": t(state, "referrals"), "callback_data": "referrals"}])
    account = TelegramAccount.query.filter_by(user_id=user.id).first() if user is not None else None
    if is_bot_admin(getattr(account, "telegram_id", None), getattr(account, "username", None)):
        rows.append([{"text": t(state, "admin_panel"), "callback_data": "admin_panel"}])
    rows.append([{"text": t(state, "support"), "callback_data": "help"}])
    return {"inline_keyboard": rows}


def is_payment_method_enabled(method: str) -> bool:
    cfg = PAYMENT_METHODS.get(method) or {}
    return all((os.environ.get(name) or "").strip() for name in cfg.get("env", ()))


def payment_methods_keyboard(plan_months: int, state: BotUserState) -> dict[str, object]:
    rows: list[list[tuple[str, str]]] = []
    enabled_methods = [(PAYMENT_METHODS[code]["label"], f"pm_{code}_{plan_months}") for code in ("platega", "cryptobot", "heleket", "crystal") if is_payment_method_enabled(code)]
    for item in enabled_methods:
        rows.append([item])
    rows.append([(t(state, "back_plans"), "plans")])
    return keyboard(rows)


def plans_keyboard(state: BotUserState, user: User | None = None) -> dict[str, object]:
    rows: list[list[tuple[str, str]]] = []
    if user is not None and is_trial_eligible(user):
        rows.append([(t(state, "trial_offer"), "trial_activate")])
    rows.extend([[(t(state, "plan_1"), "buy_1")], [(t(state, "plan_3"), "buy_3")], [(t(state, "plan_12"), "buy_12")], [(t(state, "back"), "menu")]])
    return keyboard(rows)


def subscription_keyboard(state: BotUserState, has_active_subscription: bool = True) -> dict[str, object]:
    if not has_active_subscription:
        return keyboard(
            [
                [(t(state, "subscription_buy"), "plans")],
                [(t(state, "subscription_refresh"), "subscription_refresh")],
                [(t(state, "back_menu"), "menu")],
            ]
        )
    return keyboard(
        [
            [(t(state, "subscription_connect"), "connect"), (t(state, "subscription_qr"), "subscription_qr")],
            [(t(state, "subscription_refresh"), "subscription_refresh")],
            [(t(state, "back_menu"), "menu")],
        ]
    )


def qr_keyboard(state: BotUserState) -> dict[str, object]:
    return keyboard([[(t(state, "back"), "subscription")]])


def profile_keyboard(state: BotUserState) -> dict[str, object]:
    return subscription_keyboard(state)


def help_keyboard(state: BotUserState) -> dict[str, object]:
    prefix = "/en" if state.lang == "en" else ""
    return {
        "inline_keyboard": [
            [{"text": t(state, "help_support"), "url": f"https://t.me/{SUPPORT_USERNAME}"}, {"text": t(state, "help_faq"), "url": site_url(f"{prefix}/faq")}],
            [{"text": t(state, "help_legal"), "callback_data": "help_legal"}],
            [{"text": t(state, "change_lang"), "callback_data": "language_help"}],
            [{"text": t(state, "back"), "callback_data": "menu"}],
        ]
    }


def legal_keyboard(state: BotUserState) -> dict[str, object]:
    prefix = "/en" if state.lang == "en" else ""
    return {
        "inline_keyboard": [
            [{"text": t(state, "legal_terms"), "url": site_url(f"{prefix}/terms")}],
            [{"text": t(state, "legal_privacy"), "url": site_url(f"{prefix}/privacy-policy")}],
            [{"text": t(state, "legal_refund"), "url": site_url(f"{prefix}/refund-policy")}],
            [{"text": t(state, "legal_aup"), "url": site_url(f"{prefix}/aup")}],
            [{"text": t(state, "back"), "callback_data": "help"}],
        ]
    }


def language_keyboard(state: BotUserState) -> dict[str, object]:
    back_target = "help" if state.pending_action == "help" else "profile"
    return keyboard([[("🇷🇺 Русский", "lang_ru"), ("🇺🇸 English", "lang_en")], [(t(state, "back"), back_target)]])


def first_language_keyboard() -> dict[str, object]:
    return keyboard([[("🇷🇺 Русский", "lang_ru"), ("🇺🇸 English", "lang_en")]])

def connect_device_keyboard(state: BotUserState) -> dict[str, object]:
    return keyboard([[(t(state, "device_ios"), "connect_device_ios"), (t(state, "device_android"), "connect_device_android")], [(t(state, "device_windows"), "connect_device_windows"), (t(state, "device_macos"), "connect_device_macos")], [(t(state, "back_menu"), "menu")]])


def connect_client_keyboard(device_code: str, state: BotUserState) -> dict[str, object]:
    options = [(client["name"], f"connect_client_{device_code}_{client['code']}") for client in CONNECT_CLIENTS.get(device_code, [])]
    rows: list[list[tuple[str, str]]] = []
    for idx in range(0, len(options), 2):
        rows.append(options[idx : idx + 2])
    rows.append([(t(state, "back"), "connect")])
    return keyboard(rows)


def connect_install_keyboard(device_code: str, client_code: str, state: BotUserState) -> dict[str, object]:
    client = next((item for item in CONNECT_CLIENTS.get(device_code, []) if item["code"] == client_code), None)
    if client and client.get("url_ru") and client.get("url_global"):
        return {
            "inline_keyboard": [
                [{"text": "App Store RU", "url": client["url_ru"]}, {"text": "App Store Global", "url": client["url_global"]}],
                [{"text": t(state, "installed_app"), "callback_data": f"connect_ready_{device_code}_{client_code}"}],
                [{"text": t(state, "back"), "callback_data": f"connect_device_{device_code}"}],
            ]
        }
    download_url = client_download_url(client, state) if client else None
    if client and download_url:
        return {
            "inline_keyboard": [
                [{"text": t(state, "download_app"), "url": download_url}],
                [{"text": t(state, "installed_app"), "callback_data": f"connect_ready_{device_code}_{client_code}"}],
                [{"text": t(state, "back"), "callback_data": f"connect_device_{device_code}"}],
            ]
        }
    return keyboard([[(t(state, "installed_app"), f"connect_ready_{device_code}_{client_code}")], [(t(state, "back"), f"connect_device_{device_code}")]])


def referral_keyboard(link: str, state: BotUserState) -> dict[str, object]:
    share_text = (
        "⚡ Join me on fast and reliable Vexnd VPN using my link and get +3 free days!"
        if state.lang == "en"
        else "⚡ Подключайся к быстрому и надежному Vexnd VPN по моей ссылке и получи +3 бесплатных дня использования!"
    )
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}"
    return {"inline_keyboard": [[{"text": t(state, "share_link"), "url": share_url}], [{"text": t(state, "back_menu"), "callback_data": "menu"}]]}


def telegram_auth_confirm_keyboard(code: str, state: BotUserState) -> dict[str, object]:
    return keyboard(
        [
            [(t(state, "telegram_auth_confirm"), f"tg_auth_confirm_{code}")],
            [(t(state, "telegram_auth_decline"), f"tg_auth_decline_{code}")],
            [(t(state, "back_menu"), "menu")],
        ]
    )


def admin_panel_keyboard(state: BotUserState) -> dict[str, object]:
    return keyboard(
        [
            [(t(state, "admin_create_link"), "admin_create_link")],
            [(t(state, "admin_refresh_stats"), "admin_panel")],
            [(t(state, "back_menu"), "menu")],
        ]
    )


def admin_link_name_keyboard(state: BotUserState) -> dict[str, object]:
    return keyboard([[(t(state, "back_menu"), "menu")]])
