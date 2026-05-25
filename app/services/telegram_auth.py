from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from app.bot.models import TelegramAccount
from app.core.extensions import db
from app.domain.models import PaymentIntent, ReferralCode, ReferralFingerprint, ReferralSignup, Subscription, SubscriptionNotificationLog, TelegramAuthChallenge, TrialGrant, User, UserCouponRedemption, UserSecurity
from app.services.remnawave import is_telegram_placeholder_email
from app.services.subscriptions import ensure_remnawave_subscription_url


CHALLENGE_TTL_MINUTES = 10


def _utcnow() -> datetime:
    return datetime.utcnow()


def _new_code() -> str:
    return secrets.token_urlsafe(18).replace("-", "").replace("_", "")


def _cleanup_expired_challenges() -> None:
    now = _utcnow()
    TelegramAuthChallenge.query.filter(TelegramAuthChallenge.expires_at < now).delete(synchronize_session=False)


def create_telegram_auth_challenge(*, purpose: str, target_user_id: int | None = None) -> TelegramAuthChallenge:
    challenge = TelegramAuthChallenge(
        code=_new_code(),
        purpose=purpose,
        target_user_id=target_user_id,
        expires_at=_utcnow() + timedelta(minutes=CHALLENGE_TTL_MINUTES),
    )
    db.session.add(challenge)
    _cleanup_expired_challenges()
    db.session.commit()
    return challenge


def get_active_challenge(code: str | None, *, purpose: str | None = None) -> TelegramAuthChallenge | None:
    normalized = (code or "").strip()
    if not normalized:
        return None
    challenge = TelegramAuthChallenge.query.filter_by(code=normalized).first()
    if not challenge:
        return None
    now = _utcnow()
    if challenge.expires_at <= now or challenge.consumed_at is not None:
        return None
    if purpose and challenge.purpose != purpose:
        return None
    return challenge


def approve_telegram_auth_challenge(code: str | None, telegram_id: int) -> tuple[bool, str, TelegramAuthChallenge | None]:
    challenge = get_active_challenge(code)
    if not challenge:
        return False, "challenge_not_found", None
    if challenge.status_reason == "declined":
        challenge.status_reason = None
        db.session.commit()
    account = TelegramAccount.query.filter_by(telegram_id=int(telegram_id)).first()
    if not account:
        return False, "telegram_not_linked", None
    if challenge.purpose == "link":
        if not challenge.target_user_id:
            challenge.status_reason = "challenge_invalid"
            db.session.commit()
            return False, "challenge_invalid", challenge
        if account.user_id != challenge.target_user_id:
            merged, merge_reason = merge_user_into_target(account.user_id, challenge.target_user_id)
            if not merged:
                challenge.status_reason = merge_reason
                db.session.commit()
                return False, merge_reason, challenge
            account.user_id = challenge.target_user_id
    if challenge.purpose == "password_reset":
        if not challenge.target_user_id or int(account.user_id) != int(challenge.target_user_id):
            challenge.status_reason = "challenge_invalid"
            db.session.commit()
            return False, "challenge_invalid", challenge
    challenge.telegram_id = int(telegram_id)
    challenge.approved_user_id = int(account.user_id)
    challenge.status_reason = None
    challenge.approved_at = _utcnow()
    db.session.commit()
    try:
        user = db.session.get(User, int(account.user_id))
        subscription = Subscription.query.filter_by(user_id=account.user_id).first()
        if user and subscription and subscription.is_active and subscription.expiry_date and subscription.expiry_date > _utcnow():
            ensure_remnawave_subscription_url(user, subscription)
    except Exception:
        pass
    return True, "ok", challenge


def decline_telegram_auth_challenge(code: str | None) -> tuple[bool, str, TelegramAuthChallenge | None]:
    challenge = get_active_challenge(code)
    if not challenge:
        return False, "challenge_not_found", None
    challenge.status_reason = "declined"
    challenge.approved_at = None
    challenge.approved_user_id = None
    challenge.telegram_id = None
    db.session.commit()
    return True, "declined", challenge


def consume_approved_challenge(code: str | None, *, purpose: str | None = None) -> tuple[bool, str, User | None]:
    challenge = get_active_challenge(code, purpose=purpose)
    if not challenge:
        return False, "challenge_not_found", None
    if challenge.approved_at is None or not challenge.approved_user_id:
        return False, "pending", None
    user = db.session.get(User, int(challenge.approved_user_id))
    if not user:
        return False, "user_not_found", None
    challenge.consumed_at = _utcnow()
    db.session.commit()
    return True, "ok", user


def merge_user_into_target(source_user_id: int, target_user_id: int) -> tuple[bool, str]:
    if int(source_user_id) == int(target_user_id):
        return True, "ok"
    source = db.session.get(User, int(source_user_id))
    target = db.session.get(User, int(target_user_id))
    if not source or not target:
        return False, "user_not_found"
    if not is_telegram_placeholder_email(source.email):
        return False, "merge_requires_support"
    try:
        source_sub = Subscription.query.filter_by(user_id=source.id).first()
        target_sub = Subscription.query.filter_by(user_id=target.id).first()
        if source_sub:
            if target_sub:
                source_expiry = source_sub.expiry_date or datetime.min
                target_expiry = target_sub.expiry_date or datetime.min
                if source_expiry > target_expiry:
                    target_sub.expiry_date = source_sub.expiry_date
                    target_sub.is_active = source_sub.is_active
                if not target_sub.subscription_url and source_sub.subscription_url:
                    target_sub.subscription_url = source_sub.subscription_url
                if not target_sub.sub_id and source_sub.sub_id:
                    target_sub.sub_id = source_sub.sub_id
                db.session.delete(source_sub)
            else:
                source_sub.user_id = target.id
        PaymentIntent.query.filter_by(user_id=source.id).update({"user_id": target.id}, synchronize_session=False)
        for notification in SubscriptionNotificationLog.query.filter_by(user_id=source.id).all():
            duplicate = SubscriptionNotificationLog.query.filter_by(
                user_id=target.id,
                notification_type=notification.notification_type,
                expiry_date=notification.expiry_date,
            ).first()
            if duplicate:
                db.session.delete(notification)
            else:
                notification.user_id = target.id
        source_trial = TrialGrant.query.filter_by(user_id=source.id).first()
        target_trial = TrialGrant.query.filter_by(user_id=target.id).first()
        if source_trial:
            if target_trial:
                db.session.delete(source_trial)
            else:
                source_trial.user_id = target.id
        UserSecurity.query.filter_by(user_id=source.id).delete(synchronize_session=False)
        for redemption in UserCouponRedemption.query.filter_by(user_id=source.id).all():
            duplicate = UserCouponRedemption.query.filter_by(user_id=target.id, coupon_code=redemption.coupon_code).first()
            if duplicate:
                db.session.delete(redemption)
            else:
                redemption.user_id = target.id
        for fingerprint in ReferralFingerprint.query.filter_by(referred_user_id=source.id).all():
            if ReferralFingerprint.query.filter_by(referred_user_id=target.id).first():
                db.session.delete(fingerprint)
            else:
                fingerprint.referred_user_id = target.id
        for signup in ReferralSignup.query.filter_by(referred_user_id=source.id).all():
            duplicate = ReferralSignup.query.filter_by(referred_user_id=target.id).first()
            if duplicate:
                db.session.delete(signup)
            else:
                signup.referred_user_id = target.id
        ReferralSignup.query.filter_by(referrer_user_id=source.id).update({"referrer_user_id": target.id}, synchronize_session=False)
        source_referral = ReferralCode.query.filter_by(user_id=source.id).first()
        target_referral = ReferralCode.query.filter_by(user_id=target.id).first()
        if source_referral:
            if target_referral:
                db.session.delete(source_referral)
            else:
                source_referral.user_id = target.id
        TelegramAuthChallenge.query.filter_by(target_user_id=source.id).update({"target_user_id": target.id}, synchronize_session=False)
        TelegramAuthChallenge.query.filter_by(approved_user_id=source.id).update({"approved_user_id": target.id}, synchronize_session=False)
        TelegramAccount.query.filter_by(user_id=source.id).update({"user_id": target.id}, synchronize_session=False)
        db.session.delete(source)
        db.session.commit()
        return True, "ok"
    except Exception:
        db.session.rollback()
        return False, "merge_failed"
