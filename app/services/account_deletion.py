from __future__ import annotations

from sqlalchemy import or_

from app.bot.models import BotPromoRedemption, BotUserState, TelegramAccount
from app.core.extensions import db
from app.domain.models import (
    PaymentIntent,
    PaymentIntentPricing,
    ReferralCode,
    ReferralFingerprint,
    ReferralSignup,
    Subscription,
    SubscriptionNotificationLog,
    TelegramAuthChallenge,
    TrialGrant,
    User,
    UserCouponRedemption,
    UserSecurity,
    WebSession,
)
from app.services.remnawave import delete_remnawave_user_for_local_user


def delete_user_account(user_id: int) -> bool:
    user = db.session.get(User, int(user_id))
    if not user:
        return False

    delete_remnawave_user_for_local_user(user)

    telegram_account = TelegramAccount.query.filter_by(user_id=user.id).first()
    telegram_id = int(telegram_account.telegram_id) if telegram_account else None
    intent_tokens = [
        token
        for (token,) in db.session.query(PaymentIntent.token).filter(PaymentIntent.user_id == user.id).all()
        if token
    ]

    try:
        if intent_tokens:
            PaymentIntentPricing.query.filter(PaymentIntentPricing.intent_token.in_(intent_tokens)).delete(synchronize_session=False)

        SubscriptionNotificationLog.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        TrialGrant.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        UserSecurity.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        WebSession.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        UserCouponRedemption.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        ReferralFingerprint.query.filter_by(referred_user_id=user.id).delete(synchronize_session=False)
        ReferralSignup.query.filter(
            or_(ReferralSignup.referrer_user_id == user.id, ReferralSignup.referred_user_id == user.id)
        ).delete(synchronize_session=False)
        ReferralCode.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        TelegramAuthChallenge.query.filter(
            or_(TelegramAuthChallenge.target_user_id == user.id, TelegramAuthChallenge.approved_user_id == user.id)
        ).delete(synchronize_session=False)
        Subscription.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        PaymentIntent.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        if telegram_id is not None:
            BotPromoRedemption.query.filter_by(telegram_id=telegram_id).delete(synchronize_session=False)
            BotUserState.query.filter_by(telegram_id=telegram_id).delete(synchronize_session=False)

        TelegramAccount.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.delete(user)
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        raise
