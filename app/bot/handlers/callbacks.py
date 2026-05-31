from __future__ import annotations

from datetime import datetime

from app.bot.common import (
    BOT_PLAN_CATALOG,
    app,
    answer_callback,
    browser_import_url,
    clear_pending_action,
    client_meta,
    db,
    edit_photo_caption,
    device_title,
    edit_message,
    h,
    make_qr_png,
    replace_message_with_screen,
    send_photo,
    send_message,
    show_loading,
    t,
    utc_now,
    get_or_create_account,
    get_or_create_state,
)
from app.bot.keyboards import (
    admin_link_menu_keyboard,
    admin_link_name_keyboard,
    admin_link_percent_keyboard,
    admin_link_stats_keyboard,
    admin_links_keyboard,
    admin_panel_keyboard,
    balance_topup_amounts_keyboard,
    balance_topup_methods_keyboard,
    connect_client_keyboard,
    connect_install_keyboard,
    help_keyboard,
    keyboard,
    legal_keyboard,
    language_keyboard,
    main_menu,
    payment_methods_keyboard_for_user,
    plans_keyboard,
    profile_keyboard,
    qr_keyboard,
    referral_keyboard,
    subscription_keyboard,
)
from app.services.bot_admin_links import format_commission_percent, is_bot_admin, tracked_link_details, tracked_link_report
from app.bot.payments import (
    balance_shortfall_cents,
    handle_balance_topup_method,
    handle_payment_check,
    handle_payment_method,
    render_balance_shortfall_text,
    render_balance_text,
)
from app.services.balance import format_balance_cents
from app.bot.subscriptions import (
    build_bot_referral_link,
    connect_intro_text,
    format_referral_text,
    format_subscription,
    invalidate_remnawave_snapshot,
    remnawave_subscription_snapshot,
    render_profile_text,
    render_subscription_text,
    schedule_connect_message_refresh,
    schedule_subscription_message_refresh,
    snapshot_has_missing_remote_subscription,
    subscription_markup,
    user_has_local_subscription_data,
)
from app.services.subscriptions import activate_trial_subscription, is_trial_eligible
from app.services.telegram_auth import approve_telegram_auth_challenge, decline_telegram_auth_challenge


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


def _format_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"


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


def handle_buy(chat_id: int, message_id: int, user, state, data: str) -> None:
    plan_months = int(data.removeprefix("buy_"))
    if plan_months not in BOT_PLAN_CATALOG:
        edit_message(chat_id, message_id, "⚠️ Invalid plan. Choose one of the available options:" if state.lang == "en" else "⚠️ Такого тарифа нет. Выберите один из доступных вариантов:", plans_keyboard(state, user))
        return
    from app.bot.common import bot_plan_label, bot_plan_price_usd, money_amount

    edit_photo_caption(
        chat_id,
        message_id,
        (
            f"{t(state, 'payment_title')}\n\n"
            f"📦 {t(state, 'plan')}: <b>{h(bot_plan_label(plan_months, state))}</b>\n"
            f"💰 {t(state, 'amount')}: <b>{money_amount(bot_plan_price_usd(plan_months))}</b>\n\n"
            f"{t(state, 'choose_payment')}"
        ),
        payment_methods_keyboard_for_user(plan_months, state, user),
    )


def handle_trial_activation(chat_id: int, message_id: int, user, state) -> None:
    if not is_trial_eligible(user):
        replace_message_with_screen(chat_id, message_id, "menu", t(state, "trial_used"), main_menu(state, user))
        return
    try:
        activate_trial_subscription(user, source="telegram", days=1)
        invalidate_remnawave_snapshot(user.id)
    except ValueError:
        db.session.rollback()
        replace_message_with_screen(chat_id, message_id, "menu", t(state, "trial_used"), main_menu(state, user))
        return
    except Exception as exc:
        db.session.rollback()
        print(f"Trial activation failed: {exc}")
        replace_message_with_screen(chat_id, message_id, "menu", t(state, "invoice_error"), main_menu(state, user))
        return

    text_out, _ = format_subscription(user, state, force_refresh=True)
    replace_message_with_screen(chat_id, message_id, "subscription", f"{t(state, 'trial_started')}\n\n{text_out}", subscription_keyboard(state))


def handle_callback(callback: dict[str, object]) -> None:
    callback_id = callback["id"]
    data = str(callback.get("data") or "")
    message = callback.get("message") or {}
    chat_id = int(message.get("chat", {}).get("id"))
    message_id = int(message.get("message_id"))
    tg_user = callback.get("from") or {}

    with app.app_context():
        account, user, _account_created = get_or_create_account(tg_user)
        state = get_or_create_state(tg_user)
        is_admin = is_bot_admin(account.telegram_id, account.username)

        if data == "menu":
            answer_callback(callback_id)
            clear_pending_action(state)
            replace_message_with_screen(chat_id, message_id, "menu", t(state, "menu_title"), main_menu(state, user))
            return
        if data == "admin_panel":
            answer_callback(callback_id)
            if not is_admin:
                edit_message(chat_id, message_id, t(state, "admin_access_denied"), main_menu(state, user))
                return
            clear_pending_action(state)
            items = tracked_link_report(limit=20)
            edit_message(chat_id, message_id, _admin_panel_text(state), admin_links_keyboard(items, state) if items else admin_panel_keyboard(state))
            return
        if data == "admin_create_link":
            answer_callback(callback_id)
            if not is_admin:
                edit_message(chat_id, message_id, t(state, "admin_access_denied"), main_menu(state, user))
                return
            state.pending_action = "admin_link_name"
            state.updated_at = utc_now()
            db.session.commit()
            edit_message(chat_id, message_id, t(state, "admin_create_prompt"), admin_link_name_keyboard(state))
            return
        if data.startswith("admin_link_") and data.count("_") == 2:
            answer_callback(callback_id)
            if not is_admin:
                edit_message(chat_id, message_id, t(state, "admin_access_denied"), main_menu(state, user))
                return
            clear_pending_action(state)
            link_id = int(data.rsplit("_", 1)[1])
            details = tracked_link_details(link_id)
            if not details:
                edit_message(chat_id, message_id, t(state, "admin_link_not_found"), admin_panel_keyboard(state))
                return
            edit_message(chat_id, message_id, _admin_link_card_text(state, details), admin_link_menu_keyboard(link_id, state))
            return
        if data.startswith("admin_link_stats_"):
            answer_callback(callback_id)
            if not is_admin:
                edit_message(chat_id, message_id, t(state, "admin_access_denied"), main_menu(state, user))
                return
            clear_pending_action(state)
            link_id = int(data.rsplit("_", 1)[1])
            details = tracked_link_details(link_id)
            if not details:
                edit_message(chat_id, message_id, t(state, "admin_link_not_found"), admin_panel_keyboard(state))
                return
            edit_message(chat_id, message_id, _admin_link_stats_text(state, details), admin_link_stats_keyboard(link_id, state))
            return
        if data.startswith("admin_link_percent_"):
            answer_callback(callback_id)
            if not is_admin:
                edit_message(chat_id, message_id, t(state, "admin_access_denied"), main_menu(state, user))
                return
            link_id = int(data.rsplit("_", 1)[1])
            details = tracked_link_details(link_id)
            if not details:
                edit_message(chat_id, message_id, t(state, "admin_link_not_found"), admin_panel_keyboard(state))
                return
            state.pending_action = f"admin_link_percent:{link_id}"
            state.updated_at = utc_now()
            db.session.commit()
            edit_message(chat_id, message_id, t(state, "admin_percent_prompt", name=h(details["name"])), admin_link_percent_keyboard(link_id, state))
            return
        if data == "plans":
            answer_callback(callback_id)
            clear_pending_action(state)
            replace_message_with_screen(chat_id, message_id, "payment", t(state, "choose_plan"), plans_keyboard(state, user))
            return
        if data == "trial_activate":
            answer_callback(callback_id)
            clear_pending_action(state)
            handle_trial_activation(chat_id, message_id, user, state)
            return
        if data == "profile":
            answer_callback(callback_id)
            clear_pending_action(state)
            replace_message_with_screen(chat_id, message_id, "profile", render_profile_text(user, state), profile_keyboard(state))
            return
        if data in ("subscription", "subscription_refresh"):
            answer_callback(callback_id)
            clear_pending_action(state)
            force_refresh = data == "subscription_refresh"
            has_local_data = user_has_local_subscription_data(user)
            if not force_refresh and not has_local_data:
                show_loading(chat_id, message_id, state)
            snapshot = remnawave_subscription_snapshot(user, force_refresh=force_refresh, schedule_async_refresh=not has_local_data)
            text_out, _ = render_subscription_text(snapshot, state)
            if force_refresh:
                edit_photo_caption(chat_id, message_id, text_out, subscription_markup(snapshot, state))
            else:
                result = replace_message_with_screen(chat_id, message_id, "subscription", text_out, subscription_markup(snapshot, state))
            if not force_refresh and has_local_data:
                new_message = (result or {}).get("result") if isinstance(result, dict) else None
                new_message_id = (new_message or {}).get("message_id") if isinstance(new_message, dict) else None
                schedule_subscription_message_refresh(user, state, chat_id, int(new_message_id or message_id), text_out)
            return
        if data in ("info", "help"):
            answer_callback(callback_id)
            state.pending_action = "help"
            state.updated_at = utc_now()
            db.session.commit()
            edit_message(chat_id, message_id, f"{t(state, 'help_title')}\n\n{t(state, 'help_text')}", help_keyboard(state))
            return
        if data == "help_legal":
            answer_callback(callback_id)
            state.pending_action = "help"
            state.updated_at = utc_now()
            db.session.commit()
            edit_message(chat_id, message_id, f"{t(state, 'legal_title')}\n\n{t(state, 'legal_text')}", legal_keyboard(state))
            return
        if data == "language_help":
            answer_callback(callback_id)
            state.pending_action = "help"
            state.updated_at = utc_now()
            db.session.commit()
            edit_message(chat_id, message_id, "🌐 <b>Language / Язык</b>", language_keyboard(state))
            return
        if data == "referrals":
            answer_callback(callback_id)
            clear_pending_action(state)
            link = build_bot_referral_link(user)
            replace_message_with_screen(chat_id, message_id, "referrals", format_referral_text(user, state), referral_keyboard(link, state))
            return
        if data == "balance_topup":
            answer_callback(callback_id)
            clear_pending_action(state)
            replace_message_with_screen(
                chat_id,
                message_id,
                "payment",
                f"{render_balance_text(state, user)}\n\n{t(state, 'balance_choose_amount')}",
                balance_topup_amounts_keyboard(state),
            )
            return
        if data == "balance_amount_custom":
            answer_callback(callback_id)
            state.pending_action = "balance_custom_amount"
            state.updated_at = utc_now()
            db.session.commit()
            edit_message(chat_id, message_id, t(state, "balance_custom_amount_prompt"), keyboard([[(t(state, "back"), "profile")]]))
            return
        if data.startswith("balance_shortfall_"):
            answer_callback(callback_id)
            plan_months = int(data.removeprefix("balance_shortfall_"))
            amount_cents = balance_shortfall_cents(user, plan_months)
            if amount_cents <= 0:
                edit_photo_caption(chat_id, message_id, t(state, "balance_shortfall_cleared"), payment_methods_keyboard_for_user(plan_months, state, user))
                return
            edit_photo_caption(
                chat_id,
                message_id,
                f"{render_balance_shortfall_text(state, user, plan_months)}\n\n{t(state, 'balance_choose_method')}\n💰 <b>{format_balance_cents(amount_cents)}</b>",
                balance_topup_methods_keyboard(amount_cents, state, back_callback=f"buy_{plan_months}"),
            )
            return
        if data.startswith("balance_amount_"):
            answer_callback(callback_id)
            amount_cents = int(data.removeprefix("balance_amount_"))
            edit_photo_caption(chat_id, message_id, f"{render_balance_text(state, user)}\n\n{t(state, 'balance_choose_method')}\n💰 <b>{amount_cents / 100:.2f} USD</b>", balance_topup_methods_keyboard(amount_cents, state))
            return
        if data in ("setup", "connect"):
            answer_callback(callback_id)
            clear_pending_action(state)
            has_local_data = user_has_local_subscription_data(user)
            if not has_local_data:
                show_loading(chat_id, message_id, state)
            text_out, markup = connect_intro_text(user, state, schedule_async_refresh=not has_local_data)
            result = replace_message_with_screen(chat_id, message_id, "connect", text_out, markup)
            if has_local_data:
                new_message = (result or {}).get("result") if isinstance(result, dict) else None
                new_message_id = (new_message or {}).get("message_id") if isinstance(new_message, dict) else None
                schedule_connect_message_refresh(user, state, chat_id, int(new_message_id or message_id), text_out)
            return
        if data.startswith("connect_device_"):
            answer_callback(callback_id)
            device_code = data.removeprefix("connect_device_")
            device_name = device_title(device_code, state)
            edit_photo_caption(chat_id, message_id, f"{t(state, 'connect_title')}\n\n{t(state, 'connect_choose_client', device=device_name)}", connect_client_keyboard(device_code, state))
            return
        if data.startswith("connect_client_"):
            answer_callback(callback_id)
            _, _, device_code, client_code = data.split("_", 3)
            client = client_meta(device_code, client_code)
            if not client:
                edit_message(chat_id, message_id, t(state, "invoice_missing"), main_menu(state, user))
                return
            edit_photo_caption(chat_id, message_id, f"{t(state, 'connect_title')}\n\n{t(state, 'connect_install_step', client=client['name'], device=device_title(device_code, state))}", connect_install_keyboard(device_code, client_code, state))
            return
        if data.startswith("connect_ready_"):
            answer_callback(callback_id)
            _, _, device_code, client_code = data.split("_", 3)
            client = client_meta(device_code, client_code)
            snapshot = remnawave_subscription_snapshot(user)
            sub_url = str(snapshot.get("subscription_url") or "").strip()
            if not client or not sub_url:
                text_key = "subscription_missing" if snapshot_has_missing_remote_subscription(snapshot) else "connect_link_missing"
                edit_photo_caption(chat_id, message_id, t(state, text_key), subscription_markup(snapshot, state))
                return
            add_url = browser_import_url(user, client, state, sub_url)
            markup = {
                "inline_keyboard": [
                    [{"text": t(state, "add_to_app"), "url": add_url}],
                    [{"text": t(state, "done"), "callback_data": "subscription"}],
                    [{"text": t(state, "back"), "callback_data": f"connect_client_{device_code}_{client_code}"}],
                ]
            }
            edit_photo_caption(chat_id, message_id, f"{t(state, 'connect_title')}\n\n{t(state, 'connect_add_step', client=client['name'])}\n\n🔗 <b>{t(state, 'sub_link')}</b>\n<code>{h(sub_url)}</code>\n\n{t(state, 'connect_done')}", markup)
            return
        if data == "language":
            answer_callback(callback_id)
            edit_message(chat_id, message_id, "🌐 <b>Language / Язык</b>", language_keyboard(state))
            return
        if data in ("lang_ru", "lang_en"):
            answer_callback(callback_id)
            previous_action = state.pending_action
            state.lang = "ru" if data == "lang_ru" else "en"
            state.pending_action = None
            state.updated_at = utc_now()
            db.session.commit()
            if previous_action == "help":
                edit_message(chat_id, message_id, f"{t(state, 'help_title')}\n\n{t(state, 'help_text')}", help_keyboard(state))
                return
            replace_message_with_screen(chat_id, message_id, "menu", t(state, "menu_title"), main_menu(state, user))
            return
        if data == "subscription_qr":
            answer_callback(callback_id)
            snapshot = remnawave_subscription_snapshot(user)
            _, sub_url = render_subscription_text(snapshot, state)
            if sub_url:
                send_photo(chat_id, make_qr_png(sub_url), t(state, "qr_caption"), qr_keyboard(state))
            else:
                text_key = "subscription_missing" if snapshot_has_missing_remote_subscription(snapshot) else "qr_unavailable"
                edit_message(chat_id, message_id, t(state, text_key), keyboard([[(t(state, "back"), "subscription")]]))
            return
        if data.startswith("tg_auth_confirm_"):
            answer_callback(callback_id)
            code = data.removeprefix("tg_auth_confirm_")
            ok, reason, _challenge = approve_telegram_auth_challenge(code, account.telegram_id)
            key = "telegram_login_ok" if ok else f"telegram_login_{reason}"
            edit_message(chat_id, message_id, t(state, key), main_menu(state, user))
            return
        if data.startswith("tg_auth_decline_"):
            answer_callback(callback_id)
            code = data.removeprefix("tg_auth_decline_")
            ok, reason, _challenge = decline_telegram_auth_challenge(code)
            key = "telegram_auth_declined" if ok else f"telegram_login_{reason}"
            edit_message(chat_id, message_id, t(state, key), main_menu(state, user))
            return
        if data.startswith("buy_"):
            answer_callback(callback_id)
            handle_buy(chat_id, message_id, user, state, data)
            return
        if data.startswith("pm_"):
            answer_callback(callback_id)
            handle_payment_method(chat_id, message_id, user, state, data)
            return
        if data.startswith("balance_pm_"):
            answer_callback(callback_id)
            handle_balance_topup_method(chat_id, message_id, user, state, data)
            return
        if data == "promo_start":
            answer_callback(callback_id)
            state.pending_action = "promo"
            state.updated_at = utc_now()
            db.session.commit()
            edit_message(chat_id, message_id, t(state, "promo_prompt"), keyboard([[(t(state, "back"), "profile")]]))
            return
        if data.startswith("checkpay_"):
            answer_callback(callback_id)
            show_loading(chat_id, message_id, state)
            handle_payment_check(chat_id, message_id, user, state, data)
            return

        answer_callback(callback_id)
