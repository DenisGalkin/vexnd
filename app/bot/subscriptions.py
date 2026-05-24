from __future__ import annotations

import time
import threading
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.bot.common import (
    TelegramAccount,
    app,
    browser_import_url,
    coerce_int,
    edit_message,
    find_remote_value,
    format_bytes,
    h,
    send_message,
    site_url,
    t,
)
from app.core.extensions import db
from app.domain.models import ReferralCode, ReferralSignup, Subscription, SubscriptionNotificationLog, TrialGrant, User
from app.bot.keyboards import subscription_keyboard
from app.bot.models import BotUserState, utc_now
from app.services.referrals import get_or_create_referral_code
from app.services.remnawave import (
    get_remnawave_config,
    is_telegram_placeholder_email,
    parse_rw_datetime,
    remnawave_find_user,
    remnawave_uses_telegram_identity,
    rw_username_from_email,
    rw_username_from_telegram,
)
from app.services.subscriptions import ensure_remnawave_subscription_url, get_trial_grant, has_processed_plan_payment, is_trial_eligible, restore_local_subscription_state


REMNAWAVE_SNAPSHOT_TTL_SECONDS = 30
_REMNAWAVE_SNAPSHOT_CACHE: dict[int, dict[str, Any]] = {}
_REMNAWAVE_REFRESH_IN_FLIGHT: set[int] = set()
_REMNAWAVE_REFRESH_LOCK = threading.Lock()
SUBSCRIPTION_REMINDER_SOON_HOURS = 12
SUBSCRIPTION_REMINDER_EXPIRED_GRACE_HOURS = 48


def get_active_subscription(user: User) -> Subscription | None:
    subscription = Subscription.query.filter_by(user_id=user.id).first()
    if not subscription or not subscription.is_active or not subscription.expiry_date or subscription.expiry_date <= utc_now():
        return None
    return subscription


def subscription_cache_signature(subscription: Subscription | None) -> tuple[Any, ...]:
    if not subscription:
        return (None, None, None)
    return (bool(subscription.is_active), subscription.expiry_date.isoformat() if subscription.expiry_date else None, (subscription.subscription_url or "").strip())


def invalidate_remnawave_snapshot(user_id: int | None) -> None:
    if user_id:
        _REMNAWAVE_SNAPSHOT_CACHE.pop(int(user_id), None)


def local_subscription_snapshot(user: User) -> tuple[Subscription | None, dict[str, Any]]:
    subscription = Subscription.query.filter_by(user_id=user.id).first()
    is_active = bool(subscription and subscription.is_active and subscription.expiry_date and subscription.expiry_date > utc_now())
    snapshot: dict[str, Any] = {
        "active": is_active,
        "expiry_date": subscription.expiry_date if subscription else None,
        "subscription_url": (subscription.subscription_url or "").strip() if subscription else "",
        "used_bytes": None,
        "limit_bytes": None,
    }
    return subscription, snapshot


def has_local_subscription_data(snapshot: dict[str, Any]) -> bool:
    return bool(snapshot.get("active") or snapshot.get("expiry_date") or (snapshot.get("subscription_url") or "").strip())


def user_has_local_subscription_data(user: User) -> bool:
    _subscription, snapshot = local_subscription_snapshot(user)
    return has_local_subscription_data(snapshot)


def snapshot_from_remote_user(local_snapshot: dict[str, Any], remote_user: dict | None) -> dict[str, Any]:
    snapshot = dict(local_snapshot)
    remote_expiry = parse_rw_datetime((remote_user or {}).get("expireAt") or (remote_user or {}).get("expire_at"))
    if remote_expiry:
        snapshot["expiry_date"] = remote_expiry
        snapshot["active"] = bool(remote_expiry > utc_now())
    remote_sub_url = str((remote_user or {}).get("subscriptionUrl") or "").strip()
    if remote_sub_url:
        snapshot["subscription_url"] = remote_sub_url
    used_raw = find_remote_value(remote_user or {}, {"usedtrafficbytes", "usedbytes", "trafficusedbytes", "uploadbytes", "downloadbytes"})
    limit_raw = find_remote_value(remote_user or {}, {"trafficlimitbytes", "limitbytes", "totallimitbytes"})
    snapshot["used_bytes"] = coerce_int(used_raw)
    snapshot["limit_bytes"] = coerce_int(limit_raw)
    return snapshot


def render_subscription_text(snapshot: dict[str, Any], state: BotUserState) -> tuple[str, str | None]:
    expiry = snapshot.get("expiry_date")
    status_key = "subscription_status_active" if snapshot["active"] else "subscription_status_inactive"
    status_icon = "🟢" if snapshot["active"] else "🔴"
    traffic_text = t(state, "traffic_unknown")
    if snapshot.get("limit_bytes") is not None or snapshot.get("used_bytes") is not None:
        traffic_text = f"{format_bytes(snapshot.get('used_bytes'))} / {format_bytes(snapshot.get('limit_bytes'))}"
    expiry_text = expiry.strftime("%d.%m.%Y") if expiry else "—"
    text = (
        f"{t(state, 'subscription_title')}\n\n"
        f"{t(state, 'subscription_expiry')}: <b>{expiry_text}</b>\n"
        f"{t(state, 'subscription_traffic')}: <b>{traffic_text}</b>\n"
        f"{t(state, 'subscription_status')}: {status_icon} <b>{t(state, status_key)}</b>"
    )
    sub_url = (snapshot.get("subscription_url") or "").strip() or None
    if sub_url:
        text += f"\n\n🔗 <b>{t(state, 'sub_link')}</b>\n<code>{h(sub_url)}</code>"
    elif snapshot["active"]:
        text += "\n\n" + t(state, "syncing")
    else:
        text += f"\n\n{t(state, 'subscription_missing')}"
    return text, sub_url


def render_connect_text(user: User, state: BotUserState, *, schedule_async_refresh: bool = True) -> tuple[str, dict[str, Any]]:
    from app.bot.keyboards import connect_device_keyboard, keyboard

    _text, sub_url = format_subscription(user, state, schedule_async_refresh=schedule_async_refresh)
    if not get_active_subscription(user):
        return (
            f"{t(state, 'connect_title')}\n\n{t(state, 'connect_need_subscription')}",
            keyboard([[(t(state, "subscription_buy"), "plans")], [(t(state, "back_menu"), "menu")]]),
        )
    if not sub_url:
        return (
            f"{t(state, 'connect_title')}\n\n{t(state, 'connect_link_missing')}",
            keyboard([[(t(state, "subscription_refresh"), "subscription_refresh")], [(t(state, "back_menu"), "menu")]]),
        )
    return (
        f"{t(state, 'connect_title')}\n\n{t(state, 'connect_choose_device')}",
        connect_device_keyboard(state),
    )


def refresh_remnawave_snapshot_async(
    user_id: int,
    signature: tuple[Any, ...],
    *,
    chat_id: int | None = None,
    message_id: int | None = None,
    telegram_id: int | None = None,
    previous_text: str | None = None,
    render_mode: str = "subscription",
) -> None:
    user_id = int(user_id)
    with _REMNAWAVE_REFRESH_LOCK:
        if user_id in _REMNAWAVE_REFRESH_IN_FLIGHT:
            return
        _REMNAWAVE_REFRESH_IN_FLIGHT.add(user_id)

    def _worker() -> None:
        try:
            with app.app_context():
                user = db.session.get(User, user_id)
                if not user:
                    return
                cfg = get_remnawave_config()
                if not (cfg.base_url and cfg.token):
                    return
                _subscription, local_snapshot = local_subscription_snapshot(user)
                include_email = not remnawave_uses_telegram_identity(user)
                remote_user = remnawave_find_user(cfg, user, include_email=include_email)
                if not isinstance(remote_user, dict):
                    return
                restore_local_subscription_state(user, remote_user)
                refreshed_snapshot = snapshot_from_remote_user(local_snapshot, remote_user)
                _REMNAWAVE_SNAPSHOT_CACHE[user_id] = {
                    "ts": time.time(),
                    "signature": signature,
                    "snapshot": refreshed_snapshot,
                }
                if chat_id and message_id and telegram_id:
                    state = BotUserState.query.filter_by(telegram_id=telegram_id).first()
                    if state:
                        if render_mode == "connect":
                            refreshed_text, refreshed_markup = render_connect_text(user, state, schedule_async_refresh=False)
                        else:
                            refreshed_text, _sub_url = render_subscription_text(refreshed_snapshot, state)
                            refreshed_markup = subscription_keyboard(state)
                        if refreshed_text != (previous_text or ""):
                            edit_message(chat_id, message_id, refreshed_text, refreshed_markup)
        except Exception as exc:
            print(f"Remnawave async snapshot refresh failed: {exc}")
        finally:
            with _REMNAWAVE_REFRESH_LOCK:
                _REMNAWAVE_REFRESH_IN_FLIGHT.discard(user_id)

    threading.Thread(target=_worker, name=f"rw-refresh-{user_id}", daemon=True).start()


def remnawave_subscription_snapshot(user: User, *, force_refresh: bool = False, schedule_async_refresh: bool = True) -> dict[str, Any]:
    subscription, snapshot = local_subscription_snapshot(user)
    if force_refresh and snapshot["active"] and not snapshot["subscription_url"] and subscription:
        snapshot["subscription_url"] = ensure_remnawave_subscription_url(user, subscription)
        invalidate_remnawave_snapshot(user.id)
    cfg = get_remnawave_config()
    if not (cfg.base_url and cfg.token):
        return snapshot
    cache_key = int(user.id)
    signature = subscription_cache_signature(subscription)
    cached = _REMNAWAVE_SNAPSHOT_CACHE.get(cache_key)
    now_ts = time.time()
    if not force_refresh and cached and cached.get("signature") == signature and now_ts - float(cached.get("ts") or 0) < REMNAWAVE_SNAPSHOT_TTL_SECONDS:
        return dict(cached["snapshot"])
    if not force_refresh and schedule_async_refresh and has_local_subscription_data(snapshot):
        refresh_remnawave_snapshot_async(user.id, signature)
        return snapshot
    include_email = not remnawave_uses_telegram_identity(user)
    try:
        remote_user = remnawave_find_user(cfg, user, include_email=include_email)
    except Exception as exc:
        print(f"Remnawave snapshot lookup failed: {exc}")
        return snapshot
    if not isinstance(remote_user, dict):
        _REMNAWAVE_SNAPSHOT_CACHE[cache_key] = {"ts": now_ts, "signature": subscription_cache_signature(subscription), "snapshot": dict(snapshot)}
        return snapshot
    restore_local_subscription_state(user, remote_user)
    snapshot = snapshot_from_remote_user(snapshot, remote_user)
    _REMNAWAVE_SNAPSHOT_CACHE[cache_key] = {"ts": now_ts, "signature": signature, "snapshot": dict(snapshot)}
    return snapshot


def format_subscription(user: User, state: BotUserState, *, force_refresh: bool = False, schedule_async_refresh: bool = True) -> tuple[str, str | None]:
    snapshot = remnawave_subscription_snapshot(user, force_refresh=force_refresh, schedule_async_refresh=schedule_async_refresh)
    return render_subscription_text(snapshot, state)


def schedule_subscription_message_refresh(user: User, state: BotUserState, chat_id: int, message_id: int, previous_text: str) -> None:
    subscription, _snapshot = local_subscription_snapshot(user)
    refresh_remnawave_snapshot_async(
        user.id,
        subscription_cache_signature(subscription),
        chat_id=chat_id,
        message_id=message_id,
        telegram_id=state.telegram_id,
        previous_text=previous_text,
        render_mode="subscription",
    )


def format_profile(account: TelegramAccount, user: User, state: BotUserState) -> tuple[str, str | None]:
    return format_subscription(user, state)


def build_bot_referral_link(user: User) -> str:
    code = get_or_create_referral_code(user)
    from app.bot.common import telegram_bot_url

    bot_url = telegram_bot_url()
    if bot_url:
        return f"{bot_url}?start=ref_{code}"
    return site_url(f"/r/{code}")


def referral_stats(user: User) -> tuple[int, int]:
    invited, paid = (
        db.session.query(
            func.count(ReferralSignup.id),
            func.count(ReferralSignup.bonuses_applied_at),
        )
        .filter(ReferralSignup.referrer_user_id == user.id)
        .one()
    )
    return int(invited or 0), int(paid or 0)


def referral_inviter_label(user: User, state: BotUserState) -> str | None:
    signup = ReferralSignup.query.filter_by(referred_user_id=user.id).first()
    if not signup:
        return None
    referrer = db.session.get(User, signup.referrer_user_id)
    if not referrer:
        return None
    account = TelegramAccount.query.filter_by(user_id=referrer.id).first()
    if account and account.username:
        return f"@{account.username}"
    if account:
        return f"id:{account.telegram_id}"
    if is_telegram_placeholder_email(referrer.email):
        return None
    return referrer.email


def format_referral_text(user: User, state: BotUserState) -> str:
    invited, paid = referral_stats(user)
    link = build_bot_referral_link(user)
    text = (
        f"{t(state, 'referral_title')}\n\n"
        f"{t(state, 'referral_text')}\n\n"
        f"🔗 <b>{t(state, 'referral_link_label')}</b>\n<code>{h(link)}</code>\n\n"
        f"👥 {t(state, 'referral_invited')}: <b>{invited}</b>\n"
        f"💳 {t(state, 'referral_paid')}: <b>{paid}</b>\n"
        f"🎁 {t(state, 'bonus_days')}: <b>{t(state, 'referral_bonus_value')}</b>\n"
    )
    invited_by = referral_inviter_label(user, state)
    if invited_by:
        text += f"\n🤝 {t(state, 'referral_invited_by')}: <b>{h(invited_by)}</b>\n"
    text += f"\n{t(state, 'referral_bonus_note')}"
    return text


def connect_intro_text(user: User, state: BotUserState, *, schedule_async_refresh: bool = True) -> tuple[str, dict[str, Any]]:
    return render_connect_text(user, state, schedule_async_refresh=schedule_async_refresh)


def schedule_connect_message_refresh(user: User, state: BotUserState, chat_id: int, message_id: int, previous_text: str) -> None:
    subscription, _snapshot = local_subscription_snapshot(user)
    refresh_remnawave_snapshot_async(
        user.id,
        subscription_cache_signature(subscription),
        chat_id=chat_id,
        message_id=message_id,
        telegram_id=state.telegram_id,
        previous_text=previous_text,
        render_mode="connect",
    )


def user_has_completed_paid_purchase(user: User) -> bool:
    return has_processed_plan_payment(user)


def capture_bot_referral(user: User, raw_code: str) -> str | None:
    code = (raw_code or "").strip().upper()
    if not code:
        return None
    referral = ReferralCode.query.filter_by(code=code).first()
    if not referral or not referral.user_id or referral.user_id == user.id:
        return "referral_invalid"
    if ReferralSignup.query.filter_by(referred_user_id=user.id).first():
        return "referral_already_used"
    if user_has_completed_paid_purchase(user):
        return "referral_too_late"
    db.session.add(ReferralSignup(referrer_user_id=referral.user_id, referred_user_id=user.id, code_used=code))
    db.session.commit()
    return "referral_captured"


def notification_sent(user_id: int, notification_type: str, expiry_date: datetime) -> bool:
    existing = (
        db.session.query(SubscriptionNotificationLog.id)
        .filter_by(user_id=user_id, notification_type=notification_type, expiry_date=expiry_date)
        .first()
    )
    return existing is not None


def mark_notification_sent(user_id: int, telegram_id: int, notification_type: str, expiry_date: datetime) -> None:
    db.session.add(
        SubscriptionNotificationLog(
            user_id=user_id,
            telegram_id=telegram_id,
            notification_type=notification_type,
            expiry_date=expiry_date,
        )
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


def is_trial_expiry(user_id: int, expiry_date: datetime) -> bool:
    trial = TrialGrant.query.filter_by(user_id=user_id).first()
    if not trial or not trial.expires_at:
        return False
    return abs((trial.expires_at - expiry_date).total_seconds()) <= 300


def dispatch_subscription_reminders() -> None:
    from app.bot.keyboards import plans_keyboard

    now = utc_now()
    soon_delta = timedelta(hours=max(SUBSCRIPTION_REMINDER_SOON_HOURS, 1))
    expiry_cutoff = now - timedelta(hours=max(SUBSCRIPTION_REMINDER_EXPIRED_GRACE_HOURS, 1))
    rows = (
        db.session.query(TelegramAccount, Subscription, BotUserState, TrialGrant, User)
        .join(Subscription, Subscription.user_id == TelegramAccount.user_id)
        .join(User, User.id == TelegramAccount.user_id)
        .outerjoin(BotUserState, BotUserState.telegram_id == TelegramAccount.telegram_id)
        .outerjoin(TrialGrant, TrialGrant.user_id == TelegramAccount.user_id)
        .filter(Subscription.expiry_date.isnot(None))
        .all()
    )
    for account, subscription, state, trial, user in rows:
        expiry = subscription.expiry_date
        delta = expiry - now
        trial_expiry = bool(trial and trial.expires_at and abs((trial.expires_at - expiry).total_seconds()) <= 300)
        if expiry > now and delta <= soon_delta:
            reminder_type = "trial_soon" if trial_expiry else "subscription_soon"
            if notification_sent(account.user_id, reminder_type, expiry):
                continue
            hours_left = max(1, int((delta.total_seconds() + 3599) // 3600))
            text_key = "trial_reminder_soon" if trial_expiry else "subscription_reminder_soon"
            try:
                send_message(account.telegram_id, t(state, text_key, hours=hours_left), plans_keyboard(state, user))
                mark_notification_sent(account.user_id, account.telegram_id, reminder_type, expiry)
            except Exception as exc:
                db.session.rollback()
                print(f"Subscription soon reminder failed: {exc}")
            continue
        if expiry <= now and expiry >= expiry_cutoff:
            reminder_type = "trial_expired" if trial_expiry else "subscription_expired"
            if notification_sent(account.user_id, reminder_type, expiry):
                continue
            text_key = "trial_reminder_expired" if trial_expiry else "subscription_reminder_expired"
            try:
                send_message(account.telegram_id, t(state, text_key), plans_keyboard(state, user))
                mark_notification_sent(account.user_id, account.telegram_id, reminder_type, expiry)
            except Exception as exc:
                db.session.rollback()
                print(f"Subscription expired reminder failed: {exc}")
