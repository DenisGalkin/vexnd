from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from app.core.extensions import db
from app.domain.models import PaymentIntent, Subscription, TrialGrant, User
from app.services.remnawave import (
    get_remnawave_config,
    parse_rw_datetime,
    remnawave_create_user,
    remnawave_extend_user,
    remnawave_find_user,
    remnawave_subscription_url_from_user,
    remnawave_sync_user_identity,
    remnawave_update_user_traffic,
)


TRIAL_REMOTE_CHECK_TTL_SECONDS = 30
_TRIAL_REMOTE_CHECK_CACHE: dict[int, dict[str, float | bool]] = {}
_TRIAL_REMOTE_CHECK_LOCK = threading.Lock()


def restore_local_subscription_state(user: User, remote_user: dict | None) -> Subscription:
    expiry_date = parse_rw_datetime((remote_user or {}).get("expireAt") or (remote_user or {}).get("expire_at"))
    subscription_url = remnawave_subscription_url_from_user(remote_user)
    subscription = Subscription.query.filter_by(user_id=user.id).first()
    if not subscription:
        subscription = Subscription(user_id=user.id, expiry_date=expiry_date or datetime.utcnow(), subscription_url=subscription_url, is_active=bool(expiry_date and expiry_date > datetime.utcnow()))
        db.session.add(subscription)
    else:
        if expiry_date:
            subscription.expiry_date = expiry_date
        if subscription_url:
            subscription.subscription_url = subscription_url
        subscription.is_active = bool(expiry_date and expiry_date > datetime.utcnow())
    db.session.commit()
    return subscription


def create_remnawave_subscription(user: User, plan_months: int, *, strict: bool = False) -> str:
    return create_remnawave_subscription_days(user, int(plan_months) * 30, strict=strict)


def create_remnawave_subscription_days(user: User, days: int, *, strict: bool = False) -> str:
    expiry_date = datetime.utcnow() + timedelta(days=max(int(days), 1))
    cfg = get_remnawave_config()
    if not (cfg.base_url and cfg.token):
        subscription = Subscription.query.filter_by(user_id=user.id).first()
        if subscription and subscription.expiry_date and subscription.expiry_date > datetime.utcnow():
            expiry_date = subscription.expiry_date + timedelta(days=max(int(days), 1))
        if not subscription:
            subscription = Subscription(user_id=user.id, expiry_date=expiry_date, subscription_url="", is_active=True)
            db.session.add(subscription)
        else:
            subscription.expiry_date = expiry_date
            subscription.is_active = True
        db.session.commit()
        return subscription.subscription_url or ""
    try:
        remote_user = remnawave_find_user(cfg, user)
        if remote_user and remote_user.get("uuid"):
            remote_user = remnawave_sync_user_identity(cfg, user, remote_user=remote_user) or remote_user
            remnawave_extend_user(cfg, remote_user["uuid"], max(int(days), 1))
        else:
            remote_user = remnawave_create_user(cfg, user, expiry_date)
            if remote_user.get("uuid"):
                remnawave_update_user_traffic(cfg, remote_user["uuid"])
        subscription = restore_local_subscription_state(user, remnawave_find_user(cfg, user) or remote_user)
        return subscription.subscription_url or ""
    except Exception:
        if strict:
            raise
        subscription = Subscription.query.filter_by(user_id=user.id).first()
        if not subscription:
            subscription = Subscription(user_id=user.id, expiry_date=expiry_date, subscription_url="", is_active=True)
            db.session.add(subscription)
        else:
            if subscription.expiry_date and subscription.expiry_date > datetime.utcnow():
                expiry_date = subscription.expiry_date + timedelta(days=max(int(days), 1))
            subscription.expiry_date = expiry_date
            subscription.is_active = True
        db.session.commit()
        return subscription.subscription_url or ""


def ensure_remnawave_subscription_url(user: User, subscription: Subscription) -> str:
    cfg = get_remnawave_config()
    if not (cfg.base_url and cfg.token):
        return (subscription.subscription_url or "").strip()
    try:
        remote_user = remnawave_find_user(cfg, user)
        if remote_user and remote_user.get("uuid"):
            remote_user = remnawave_sync_user_identity(cfg, user, remote_user=remote_user) or remote_user
        if (
            not remote_user
            and subscription.is_active
            and subscription.expiry_date
            and subscription.expiry_date > datetime.utcnow()
        ):
            remote_user = remnawave_create_user(cfg, user, subscription.expiry_date)
            if remote_user.get("uuid"):
                remnawave_update_user_traffic(cfg, remote_user["uuid"])
        remote_url = remnawave_subscription_url_from_user(remote_user)
        if remote_url and remote_url != (subscription.subscription_url or "").strip():
            subscription.subscription_url = remote_url
            db.session.commit()
        return (subscription.subscription_url or "").strip()
    except Exception:
        return (subscription.subscription_url or "").strip()


def deactivate_local_subscription(subscription: Subscription | None) -> None:
    if not subscription:
        return
    changed = False
    if subscription.is_active:
        subscription.is_active = False
        changed = True
    if subscription.subscription_url:
        subscription.subscription_url = ""
        changed = True
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def extend_subscription_days_local(user: User, days: int) -> None:
    days = int(days)
    if days <= 0:
        return
    now = datetime.utcnow()
    subscription = Subscription.query.filter_by(user_id=user.id).first()
    base_dt = now
    if subscription and subscription.expiry_date and subscription.expiry_date > now:
        base_dt = subscription.expiry_date
    new_expiry = base_dt + timedelta(days=days)
    if not subscription:
        subscription = Subscription(user_id=user.id, expiry_date=new_expiry, subscription_url="", is_active=True)
        db.session.add(subscription)
    else:
        subscription.expiry_date = new_expiry
        subscription.is_active = True
    db.session.commit()


def extend_remnawave_subscription_days(user: User, days: int) -> None:
    days = int(days)
    if days <= 0:
        return
    extend_subscription_days_local(user, days)
    cfg = get_remnawave_config()
    if not (cfg.base_url and cfg.token):
        return
    try:
        remote_user = remnawave_find_user(cfg, user)
        if remote_user and remote_user.get("uuid"):
            remote_user = remnawave_sync_user_identity(cfg, user, remote_user=remote_user) or remote_user
            remnawave_extend_user(cfg, remote_user["uuid"], days)
            subscription = Subscription.query.filter_by(user_id=user.id).first()
            if subscription and not subscription.subscription_url:
                remote_user2 = remnawave_find_user(cfg, user) or {}
                subscription.subscription_url = remote_user2.get("subscriptionUrl") or subscription.subscription_url
                db.session.commit()
    except Exception as exc:
        print(f"Remnawave extend by days failed: {exc}")


def has_processed_plan_payment(user: User) -> bool:
    if not user or not getattr(user, "id", None):
        return False
    return bool(
        PaymentIntent.query.filter(
            PaymentIntent.user_id == user.id,
            PaymentIntent.plan_months > 0,
            PaymentIntent.processed_at.isnot(None),
        ).first()
    )


def get_trial_grant(user: User) -> TrialGrant | None:
    if not user or not getattr(user, "id", None):
        return None
    return TrialGrant.query.filter_by(user_id=user.id).first()


def trial_blocked_by_remote_registration(user: User, *, force_refresh: bool = False) -> bool | None:
    if not user or not getattr(user, "id", None):
        return False
    cfg = get_remnawave_config()
    if not (cfg.base_url and cfg.token):
        return False

    cache_key = int(user.id)
    now_ts = time.time()
    if not force_refresh:
        with _TRIAL_REMOTE_CHECK_LOCK:
            cached = _TRIAL_REMOTE_CHECK_CACHE.get(cache_key)
        if cached and now_ts - float(cached.get("ts") or 0) < TRIAL_REMOTE_CHECK_TTL_SECONDS:
            return bool(cached.get("blocked"))

    try:
        remote_user = remnawave_find_user(cfg, user)
    except Exception as exc:
        print(f"Trial remote eligibility check failed: {exc}")
        return None

    blocked = isinstance(remote_user, dict) and bool(remote_user)
    if blocked:
        try:
            restore_local_subscription_state(user, remote_user)
        except Exception as exc:
            print(f"Trial local subscription restore failed: {exc}")
            db.session.rollback()

    with _TRIAL_REMOTE_CHECK_LOCK:
        _TRIAL_REMOTE_CHECK_CACHE[cache_key] = {"ts": now_ts, "blocked": blocked}
    return blocked


def is_trial_eligible(user: User, *, fail_open_on_remote_error: bool = True, force_refresh: bool = False) -> bool:
    if not user or not getattr(user, "id", None):
        return False
    if get_trial_grant(user) is not None:
        return False
    if has_processed_plan_payment(user):
        return False
    subscription = Subscription.query.filter_by(user_id=user.id).first()
    if subscription and subscription.expiry_date:
        return False
    blocked_by_remote = trial_blocked_by_remote_registration(user, force_refresh=force_refresh)
    if blocked_by_remote is True:
        return False
    if blocked_by_remote is None and not fail_open_on_remote_error:
        return False
    return True


def activate_trial_subscription(user: User, *, source: str = "web", days: int = 1) -> Subscription:
    if not is_trial_eligible(user, fail_open_on_remote_error=False, force_refresh=True):
        raise ValueError("trial_not_available")
    trial_days = max(int(days), 1)
    create_remnawave_subscription_days(user, trial_days, strict=True)
    subscription = Subscription.query.filter_by(user_id=user.id).first()
    if not subscription or not subscription.expiry_date:
        raise RuntimeError("trial_activation_failed")
    db.session.add(TrialGrant(user_id=user.id, source=(source or "web")[:32], days=trial_days, expires_at=subscription.expiry_date))
    db.session.commit()
    return subscription
