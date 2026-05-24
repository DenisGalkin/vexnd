from __future__ import annotations

from app.bot.common import (
    BOT_PLAN_CATALOG,
    app,
    answer_callback,
    browser_import_url,
    clear_pending_action,
    client_meta,
    db,
    device_title,
    edit_message,
    h,
    make_qr_png,
    send_photo,
    send_message,
    show_loading,
    t,
    topup_amount_selected_text,
    utc_now,
    get_or_create_account,
    get_or_create_state,
)
from app.bot.keyboards import (
    connect_client_keyboard,
    connect_install_keyboard,
    help_keyboard,
    keyboard,
    legal_keyboard,
    language_keyboard,
    main_menu,
    payment_methods_keyboard,
    plans_keyboard,
    qr_keyboard,
    referral_keyboard,
    subscription_keyboard,
    topup_amounts_keyboard,
    topup_payment_methods_keyboard,
)
from app.bot.payments import (
    handle_balance_purchase,
    handle_payment_check,
    handle_payment_method,
    handle_topup_check,
    handle_topup_payment_method,
)
from app.bot.subscriptions import (
    build_bot_referral_link,
    connect_intro_text,
    format_referral_text,
    format_subscription,
    invalidate_remnawave_snapshot,
    remnawave_subscription_snapshot,
    schedule_connect_message_refresh,
    schedule_subscription_message_refresh,
    user_has_local_subscription_data,
)
from app.services.subscriptions import activate_trial_subscription, is_trial_eligible


def handle_buy(chat_id: int, message_id: int, user, state, data: str) -> None:
    plan_months = int(data.removeprefix("buy_"))
    if plan_months not in BOT_PLAN_CATALOG:
        edit_message(chat_id, message_id, "⚠️ Invalid plan. Choose one of the available options:" if state.lang == "en" else "⚠️ Такого тарифа нет. Выберите один из доступных вариантов:", plans_keyboard(state, user))
        return
    from app.bot.common import bot_plan_label, bot_plan_price_usd, money_amount

    edit_message(
        chat_id,
        message_id,
        (
            f"{t(state, 'payment_title')}\n\n"
            f"📦 {t(state, 'plan')}: <b>{h(bot_plan_label(plan_months, state))}</b>\n"
            f"💰 {t(state, 'amount')}: <b>{money_amount(bot_plan_price_usd(plan_months))}</b>\n\n"
            f"{t(state, 'choose_payment')}"
        ),
        payment_methods_keyboard(plan_months, state),
    )


def handle_trial_activation(chat_id: int, message_id: int, user, state) -> None:
    if not is_trial_eligible(user):
        edit_message(chat_id, message_id, t(state, "trial_used"), main_menu(state, user))
        return
    try:
        activate_trial_subscription(user, source="telegram", days=1)
        invalidate_remnawave_snapshot(user.id)
    except Exception as exc:
        db.session.rollback()
        print(f"Trial activation failed: {exc}")
        edit_message(chat_id, message_id, t(state, "invoice_error"), main_menu(state, user))
        return

    text_out, _ = format_subscription(user, state, force_refresh=True)
    edit_message(chat_id, message_id, f"{t(state, 'trial_started')}\n\n{text_out}", subscription_keyboard(state))


def handle_callback(callback: dict[str, object]) -> None:
    callback_id = callback["id"]
    data = str(callback.get("data") or "")
    message = callback.get("message") or {}
    chat_id = int(message.get("chat", {}).get("id"))
    message_id = int(message.get("message_id"))
    tg_user = callback.get("from") or {}

    with app.app_context():
        account, user = get_or_create_account(tg_user)
        state = get_or_create_state(tg_user)

        if data == "menu":
            answer_callback(callback_id)
            clear_pending_action(state)
            edit_message(chat_id, message_id, t(state, "menu_title"), main_menu(state, user))
            return
        if data == "plans":
            answer_callback(callback_id)
            clear_pending_action(state)
            edit_message(chat_id, message_id, t(state, "choose_plan"), plans_keyboard(state, user))
            return
        if data == "trial_activate":
            answer_callback(callback_id)
            clear_pending_action(state)
            handle_trial_activation(chat_id, message_id, user, state)
            return
        if data in ("profile", "subscription", "subscription_refresh"):
            answer_callback(callback_id)
            clear_pending_action(state)
            force_refresh = data == "subscription_refresh"
            has_local_data = user_has_local_subscription_data(user)
            if not force_refresh and not has_local_data:
                show_loading(chat_id, message_id, state)
            text_out, _ = format_subscription(user, state, force_refresh=force_refresh, schedule_async_refresh=not has_local_data)
            edit_message(chat_id, message_id, text_out, subscription_keyboard(state))
            if not force_refresh and has_local_data:
                schedule_subscription_message_refresh(user, state, chat_id, message_id, text_out)
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
            edit_message(chat_id, message_id, format_referral_text(user, state), referral_keyboard(link, state))
            return
        if data in ("setup", "connect"):
            answer_callback(callback_id)
            clear_pending_action(state)
            has_local_data = user_has_local_subscription_data(user)
            if not has_local_data:
                show_loading(chat_id, message_id, state)
            text_out, markup = connect_intro_text(user, state, schedule_async_refresh=not has_local_data)
            edit_message(chat_id, message_id, text_out, markup)
            if has_local_data:
                schedule_connect_message_refresh(user, state, chat_id, message_id, text_out)
            return
        if data.startswith("connect_device_"):
            answer_callback(callback_id)
            device_code = data.removeprefix("connect_device_")
            device_name = device_title(device_code, state)
            edit_message(chat_id, message_id, f"{t(state, 'connect_title')}\n\n{t(state, 'connect_choose_client', device=device_name)}", connect_client_keyboard(device_code, state))
            return
        if data.startswith("connect_client_"):
            answer_callback(callback_id)
            _, _, device_code, client_code = data.split("_", 3)
            client = client_meta(device_code, client_code)
            if not client:
                edit_message(chat_id, message_id, t(state, "invoice_missing"), main_menu(state, user))
                return
            edit_message(chat_id, message_id, f"{t(state, 'connect_title')}\n\n{t(state, 'connect_install_step', client=client['name'], device=device_title(device_code, state))}", connect_install_keyboard(device_code, client_code, state))
            return
        if data.startswith("connect_ready_"):
            answer_callback(callback_id)
            _, _, device_code, client_code = data.split("_", 3)
            client = client_meta(device_code, client_code)
            snapshot = remnawave_subscription_snapshot(user)
            sub_url = str(snapshot.get("subscription_url") or "").strip()
            if not client or not sub_url:
                edit_message(chat_id, message_id, t(state, "connect_link_missing"), subscription_keyboard(state))
                return
            add_url = browser_import_url(user, client, state, sub_url)
            markup = {
                "inline_keyboard": [
                    [{"text": t(state, "add_to_app"), "url": add_url}],
                    [{"text": t(state, "done"), "callback_data": "subscription"}],
                    [{"text": t(state, "back"), "callback_data": f"connect_client_{device_code}_{client_code}"}],
                ]
            }
            edit_message(chat_id, message_id, f"{t(state, 'connect_title')}\n\n{t(state, 'connect_add_step', client=client['name'])}\n\n🔗 <b>{t(state, 'sub_link')}</b>\n<code>{h(sub_url)}</code>\n\n{t(state, 'connect_done')}", markup)
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
            edit_message(chat_id, message_id, t(state, "menu_title"), main_menu(state, user))
            return
        if data == "subscription_qr":
            answer_callback(callback_id)
            _, sub_url = format_subscription(user, state)
            if sub_url:
                send_photo(chat_id, make_qr_png(sub_url), t(state, "qr_caption"), qr_keyboard(state))
            else:
                edit_message(chat_id, message_id, t(state, "qr_unavailable"), keyboard([[(t(state, "back"), "subscription")]]))
            return
        if data.startswith("buy_"):
            answer_callback(callback_id)
            handle_buy(chat_id, message_id, user, state, data)
            return
        if data.startswith("pm_"):
            answer_callback(callback_id)
            handle_payment_method(chat_id, message_id, user, state, data)
            return
        if data.startswith("balance_buy_"):
            answer_callback(callback_id)
            handle_balance_purchase(chat_id, message_id, user, state, data)
            return
        if data == "topup":
            answer_callback(callback_id)
            state.pending_action = None
            state.updated_at = utc_now()
            db.session.commit()
            edit_message(chat_id, message_id, t(state, "topup_title"), topup_amounts_keyboard(state))
            return
        if data == "topup_custom":
            answer_callback(callback_id, t(state, "topup_custom_hint"))
            state.pending_action = "topup_custom"
            state.updated_at = utc_now()
            db.session.commit()
            markup = keyboard([[(t(state, "back"), "topup")]])
            try:
                edit_message(chat_id, message_id, t(state, "topup_custom_prompt"), markup)
            except Exception as exc:
                print(f"Top-up custom prompt edit failed: {exc}")
                send_message(chat_id, t(state, "topup_custom_prompt"), markup)
            return
        if data.startswith("topup_amount_"):
            answer_callback(callback_id)
            amount_cents = int(data.removeprefix("topup_amount_"))
            state.pending_action = None
            state.updated_at = utc_now()
            db.session.commit()
            edit_message(chat_id, message_id, topup_amount_selected_text(state, amount_cents), topup_payment_methods_keyboard(amount_cents, state))
            return
        if data.startswith("topup_pm_"):
            answer_callback(callback_id)
            handle_topup_payment_method(chat_id, message_id, user, state, data)
            return
        if data.startswith("checktopup_"):
            answer_callback(callback_id)
            handle_topup_check(chat_id, message_id, user, state, data)
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
