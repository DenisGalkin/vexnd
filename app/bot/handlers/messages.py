from __future__ import annotations

from datetime import datetime

from app.bot.common import (
    app,
    db,
    ensure_telegram_user_password,
    get_or_create_account,
    get_or_create_state,
    h,
    send_screen,
    send_message,
    t,
    utc_now,
)
from app.bot.keyboards import (
    admin_link_menu_keyboard,
    admin_link_percent_keyboard,
    admin_link_settings_keyboard,
    admin_links_keyboard,
    admin_panel_keyboard,
    first_language_keyboard,
    help_keyboard,
    keyboard,
    main_menu,
    plans_keyboard,
    profile_keyboard,
    balance_topup_methods_keyboard,
    telegram_auth_confirm_keyboard,
)
from app.bot.subscriptions import (
    capture_bot_referral,
    remnawave_subscription_snapshot,
    render_profile_text,
    render_subscription_text,
    schedule_subscription_message_refresh,
    subscription_markup,
    user_has_local_subscription_data,
)
from app.services.balance import MAX_TOPUP_CENTS, MIN_TOPUP_CENTS, format_balance_cents
from app.services.bot_admin_links import (
    create_tracked_link,
    format_commission_percent,
    is_bot_admin,
    parse_commission_percent,
    register_tracked_link_start,
    tracked_link_details,
    tracked_link_report,
    tracked_link_url,
    update_tracked_link_commission,
)
from app.services.coupons import bot_coupon_benefits, normalize_coupon_code, record_coupon_redemption
from app.services.subscriptions import create_remnawave_subscription
from app.services.telegram_auth import approve_telegram_auth_challenge


def _format_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"


def _admin_panel_text(state) -> str:
    items = tracked_link_report(limit=20)
    if not items:
        return f"{t(state, 'admin_title')}\n\n{t(state, 'admin_empty')}"

    lines = [t(state, "admin_title"), ""]
    for item in items:
        lines.append(
            t(
                state,
                "admin_stats_line",
                name=h(item["name"]),
                unique=item["unique_starts"],
                paid_users=item["paid_users"],
                commission=h(format_balance_cents(item["commission_amount_cents"])),
            )
        )
        lines.append("")
    return "\n".join(lines).strip()


def _admin_link_card_text(state, details: dict[str, object]) -> str:
    return t(
        state,
        "admin_link_card",
        name=h(details["name"]),
        percent=h(format_commission_percent(details["commission_bps"])),
        unique=details["unique_starts"],
        paid_users=details["paid_users"],
        commission=h(format_balance_cents(details["commission_amount_cents"])),
        url=h(details["url"] or f"/start trk_{details['token']}"),
    )


def _admin_link_stats_text(state, details: dict[str, object]) -> str:
    return (
        f"{t(state, 'admin_link_stats_title', name=h(details['name']))}\n\n"
        f"{t(state, 'admin_link_stats_body', total=details['total_starts'], unique=details['unique_starts'], attributed=details['attributed_users'], paid_users=details['paid_users'], payments=details['payments_count'], paid_amount=h(format_balance_cents(details['paid_amount_cents'])), commission=h(format_balance_cents(details['commission_amount_cents'])), subscription_count=details['subscription_count'], subscription_amount=h(format_balance_cents(details['subscription_amount_cents'])), topup_count=details['balance_topup_count'], topup_amount=h(format_balance_cents(details['balance_topup_amount_cents'])), last_started=h(_format_dt(details['last_started_at'])), last_paid=h(_format_dt(details['last_paid_at'])))}"
    )


def _admin_link_settings_text(state, details: dict[str, object]) -> str:
    return t(
        state,
        "admin_link_settings_title",
        name=h(details["name"]),
        percent=h(format_commission_percent(details["commission_bps"])),
    )


def handle_promo_code(chat_id: int, user, state, raw_code: str) -> None:
    code = normalize_coupon_code(raw_code)
    state.pending_action = None
    state.updated_at = utc_now()
    benefit = bot_coupon_benefits(code, user.id if user else None)
    if benefit["error"] in {"not_found", "exhausted"}:
        db.session.commit()
        send_message(chat_id, t(state, "promo_not_found"), profile_keyboard(state))
        return
    if benefit["error"] == "already_used":
        db.session.commit()
        send_message(chat_id, t(state, "promo_used"), profile_keyboard(state))
        return
    if benefit["error"] == "checkout_only":
        db.session.commit()
        send_message(chat_id, t(state, "promo_checkout_only"), profile_keyboard(state))
        return

    record_coupon_redemption(user.id if user else None, code)
    messages: list[str] = []
    if benefit["bot_plan_months"]:
        create_remnawave_subscription(user, int(benefit["bot_plan_months"]), strict=True)
        messages.append(t(state, "promo_ok_plan", months=benefit["bot_plan_months"]))
    state.updated_at = utc_now()
    db.session.commit()
    send_message(chat_id, "\n\n".join(messages) if messages else t(state, "promo_not_found"), profile_keyboard(state))


def handle_custom_balance_amount(chat_id: int, user, state, raw_amount: str) -> None:
    normalized = (raw_amount or "").strip().replace("$", "").replace(",", ".")
    try:
        amount_value = float(normalized)
        amount_cents = int(round(amount_value * 100))
    except Exception:
        send_message(chat_id, t(state, "balance_custom_amount_invalid"), keyboard([[(t(state, "back"), "profile")]]))
        return

    if amount_cents < MIN_TOPUP_CENTS or amount_cents > MAX_TOPUP_CENTS:
        send_message(
            chat_id,
            t(
                state,
                "balance_custom_amount_out_of_range",
                min_amount=f"{MIN_TOPUP_CENTS / 100:.2f}",
                max_amount=f"{MAX_TOPUP_CENTS / 100:.2f}",
            ),
            keyboard([[(t(state, "back"), "profile")]]),
        )
        return

    state.pending_action = None
    state.updated_at = utc_now()
    db.session.commit()
    send_message(
        chat_id,
        t(state, "balance_choose_method") + f"\n💰 <b>${amount_cents / 100:.2f}</b>",
        balance_topup_methods_keyboard(amount_cents, state),
    )


def handle_message(message: dict[str, object]) -> None:
    chat = message.get("chat") or {}
    tg_user = message.get("from") or {}
    chat_id = int(chat["id"])
    text = str(message.get("text") or "").strip()
    start_arg = ""
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            start_arg = parts[1].strip()

    with app.app_context():
        account, user, account_created = get_or_create_account(tg_user)
        state = get_or_create_state(tg_user)
        is_admin = is_bot_admin(account.telegram_id, account.username)

        if start_arg.startswith("ref_"):
            result_key = capture_bot_referral(user, start_arg.removeprefix("ref_"))
            if result_key:
                send_message(chat_id, t(state, result_key))
        else:
            register_tracked_link_start(start_arg, account.telegram_id, is_first_interaction=account_created)
        if start_arg.startswith("login_") or start_arg.startswith("link_") or start_arg.startswith("password_reset_"):
            if start_arg.startswith("password_reset_"):
                purpose = "password_reset"
                code = start_arg.removeprefix("password_reset_")
            else:
                purpose, code = start_arg.split("_", 1)
            if purpose == "login":
                prompt_key = "telegram_auth_request_login"
            elif purpose == "link":
                prompt_key = "telegram_auth_request_link"
            else:
                prompt_key = "telegram_auth_request_password_reset"
            send_message(chat_id, t(state, prompt_key), telegram_auth_confirm_keyboard(code, state))
            return

        if state.pending_action == "choose_language":
            send_message(chat_id, t(state, "choose_language"), first_language_keyboard())
            return

        if state.pending_action == "promo" and text and not text.startswith("/"):
            handle_promo_code(chat_id, user, state, text)
            return
        if state.pending_action == "balance_custom_amount" and text and not text.startswith("/"):
            handle_custom_balance_amount(chat_id, user, state, text)
            return
        if state.pending_action == "admin_link_name" and text and not text.startswith("/"):
            if not is_admin:
                state.pending_action = None
                state.updated_at = utc_now()
                db.session.commit()
                send_message(chat_id, t(state, "admin_access_denied"), main_menu(state, user))
                return
            try:
                link = create_tracked_link(name=text, created_by_telegram_id=account.telegram_id)
            except ValueError as exc:
                error_key = "admin_name_too_long" if str(exc) == "name_too_long" else "admin_name_empty"
                send_message(chat_id, t(state, error_key), admin_panel_keyboard(state))
                return
            state.pending_action = None
            state.updated_at = utc_now()
            db.session.commit()
            send_message(
                chat_id,
                t(
                    state,
                    "admin_create_success",
                    name=h(link.name),
                    total=link.total_starts,
                    unique=link.unique_starts,
                    percent=h(format_commission_percent(link.commission_bps)),
                    url=h(tracked_link_url(link) or f"/start trk_{link.token}"),
                ),
                admin_link_menu_keyboard(link.id, state),
            )
            return
        if (state.pending_action or "").startswith("admin_link_percent:") and text and not text.startswith("/"):
            if not is_admin:
                state.pending_action = None
                state.updated_at = utc_now()
                db.session.commit()
                send_message(chat_id, t(state, "admin_access_denied"), main_menu(state, user))
                return
            link_id = 0
            try:
                link_id = int(state.pending_action.split(":", 1)[1])
                commission_bps = parse_commission_percent(text)
                link = update_tracked_link_commission(link_id, commission_bps)
            except ValueError:
                send_message(chat_id, t(state, "admin_percent_invalid"), admin_link_percent_keyboard(link_id, state))
                return
            state.pending_action = None
            state.updated_at = utc_now()
            db.session.commit()
            if not link:
                send_message(chat_id, t(state, "admin_link_not_found"), admin_panel_keyboard(state))
                return
            details = tracked_link_details(link.id)
            send_message(
                chat_id,
                t(state, "admin_percent_updated", name=h(link.name), percent=h(format_commission_percent(link.commission_bps)))
                + ("\n\n" + _admin_link_settings_text(state, details) if details else ""),
                admin_link_settings_keyboard(link.id, state),
            )
            return

        if text.startswith("/login "):
            ok, reason, challenge = approve_telegram_auth_challenge(text.split(maxsplit=1)[1], account.telegram_id)
            if ok and challenge and challenge.purpose == "login":
                ensure_telegram_user_password(chat_id, user, state)
            key = "telegram_login_ok" if ok else f"telegram_login_{reason}"
            send_message(chat_id, t(state, key), main_menu(state, user))
            return

        if text.startswith("/start"):
            send_screen(chat_id, "menu", t(state, "menu_title"), main_menu(state, user))
            return
        if text.startswith("/admin"):
            if not is_admin:
                send_message(chat_id, t(state, "admin_access_denied"), main_menu(state, user))
                return
            items = tracked_link_report(limit=20)
            send_message(chat_id, _admin_panel_text(state), admin_links_keyboard(items, state) if items else admin_panel_keyboard(state))
            return
        if text.startswith("/plans"):
            send_screen(chat_id, "payment", t(state, "choose_plan"), plans_keyboard(state, user))
            return
        if text.startswith("/help"):
            send_message(chat_id, t(state, "help_text"), help_keyboard(state))
            return
        if text.startswith("/profile") or text.startswith("/subscription"):
            if text.startswith("/profile"):
                send_screen(chat_id, "profile", render_profile_text(user, state), profile_keyboard(state))
                return
            has_local_data = user_has_local_subscription_data(user)
            snapshot = remnawave_subscription_snapshot(user, schedule_async_refresh=not has_local_data)
            text_out, _ = render_subscription_text(snapshot, state)
            result = send_screen(chat_id, "subscription", text_out, subscription_markup(snapshot, state))
            if has_local_data:
                message = (result or {}).get("result") if isinstance(result, dict) else None
                message_id = (message or {}).get("message_id") if isinstance(message, dict) else None
                if message_id:
                    schedule_subscription_message_refresh(user, state, chat_id, int(message_id), text_out)
            return

        send_screen(chat_id, "menu", t(state, "menu_title"), main_menu(state, user))
