from __future__ import annotations

from vexnd_app.bot.common import (
    BotPromoCode,
    BotPromoRedemption,
    app,
    db,
    get_or_create_account,
    get_or_create_state,
    parse_topup_amount_cents,
    send_message,
    t,
    topup_amount_selected_text,
    topup_invalid_amount_text,
    utc_now,
)
from vexnd_app.bot.keyboards import (
    first_language_keyboard,
    help_keyboard,
    keyboard,
    main_menu,
    plans_keyboard,
    profile_keyboard,
    subscription_keyboard,
    topup_payment_methods_keyboard,
)
from vexnd_app.bot.subscriptions import capture_bot_referral, format_profile
from vexnd_app.services.subscriptions import create_remnawave_subscription


def handle_promo_code(chat_id: int, user, state, raw_code: str) -> None:
    code = raw_code.strip().upper()
    state.pending_action = None
    state.updated_at = utc_now()
    promo = BotPromoCode.query.filter_by(code=code, is_active=True).first()
    if not promo or (promo.max_uses is not None and promo.used_count >= promo.max_uses):
        db.session.commit()
        send_message(chat_id, t(state, "promo_not_found"), profile_keyboard(state))
        return
    if BotPromoRedemption.query.filter_by(promo_id=promo.id, telegram_id=state.telegram_id).first():
        db.session.commit()
        send_message(chat_id, t(state, "promo_used"), profile_keyboard(state))
        return

    db.session.add(BotPromoRedemption(promo_id=promo.id, telegram_id=state.telegram_id))
    promo.used_count += 1
    messages: list[str] = []
    if promo.balance_cents > 0:
        state.balance_cents += promo.balance_cents
        from vexnd_app.bot.common import money

        messages.append(t(state, "promo_ok_balance", amount=money(promo.balance_cents)))
    if promo.plan_months:
        create_remnawave_subscription(user, int(promo.plan_months), strict=True)
        messages.append(t(state, "promo_ok_plan", months=promo.plan_months))
    state.updated_at = utc_now()
    db.session.commit()
    send_message(chat_id, "\n\n".join(messages) if messages else t(state, "promo_not_found"), profile_keyboard(state))


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
        account, user = get_or_create_account(tg_user)
        state = get_or_create_state(tg_user)

        if start_arg.startswith("ref_"):
            result_key = capture_bot_referral(user, start_arg.removeprefix("ref_"))
            if result_key:
                send_message(chat_id, t(state, result_key))

        if state.pending_action == "choose_language":
            send_message(chat_id, t(state, "choose_language"), first_language_keyboard())
            return

        if state.pending_action == "promo" and text and not text.startswith("/"):
            handle_promo_code(chat_id, user, state, text)
            return

        if state.pending_action == "topup_custom" and text and not text.startswith("/"):
            amount_cents = parse_topup_amount_cents(text)
            if amount_cents is None:
                send_message(chat_id, topup_invalid_amount_text(state), keyboard([[(t(state, "back"), "topup")]]))
                return
            state.pending_action = None
            state.updated_at = utc_now()
            db.session.commit()
            send_message(chat_id, topup_amount_selected_text(state, amount_cents), topup_payment_methods_keyboard(amount_cents, state))
            return

        if text.startswith("/start"):
            send_message(chat_id, t(state, "menu_title"), main_menu(state, user))
            return
        if text.startswith("/plans"):
            send_message(chat_id, t(state, "choose_plan"), plans_keyboard(state, user))
            return
        if text.startswith("/help"):
            send_message(chat_id, t(state, "help_text"), help_keyboard(state))
            return
        if text.startswith("/profile") or text.startswith("/subscription"):
            text_out, _ = format_profile(account, user, state)
            send_message(chat_id, text_out, subscription_keyboard(state))
            return

        send_message(chat_id, t(state, "menu_title"), main_menu(state, user))
