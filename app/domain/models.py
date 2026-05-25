from __future__ import annotations

from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.core.extensions import db


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(200), nullable=False)
    lang = db.Column(db.String(2), default="en")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256:120000")

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    expiry_date = db.Column(db.DateTime, nullable=False)
    subscription_url = db.Column("vless_key", db.String(500), nullable=True)
    sub_id = db.Column(db.String(64), unique=True, nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    # Add dedicated expiry_date index to speed up queries filtering by expiry_date.
    __table_args__ = (
        db.Index("ix_subscription_user_id", "user_id"),
        db.Index("ix_subscription_user_active_expiry", "user_id", "is_active", "expiry_date"),
        db.Index("ix_subscription_expiry_date", "expiry_date"),
    )


class TrialGrant(db.Model):
    __tablename__ = "trial_grant"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    source = db.Column(db.String(32), nullable=False, default="web")
    days = db.Column(db.Integer, nullable=False, default=1)
    activated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)


class SubscriptionNotificationLog(db.Model):
    __tablename__ = "subscription_notification_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    telegram_id = db.Column(db.Integer, nullable=True, index=True)
    notification_type = db.Column(db.String(32), nullable=False)
    expiry_date = db.Column(db.DateTime, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "notification_type", "expiry_date", name="uq_subscription_notification_once"),
    )


class ProcessedPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(32), nullable=False)
    external_id = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("provider", "external_id", name="uq_processed_payment_provider_external"),
    )


class PaymentIntent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(32), nullable=False)
    token = db.Column(db.String(128), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    plan_months = db.Column(db.Integer, nullable=False)
    external_id = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    processed_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index("ix_payment_intent_provider_external", "provider", "external_id"),
        db.Index("ix_payment_intent_user_processed_plan", "user_id", "processed_at", "plan_months"),
    )


class AuthThrottle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(32), nullable=False)
    key = db.Column(db.String(256), nullable=False)
    fails = db.Column(db.Integer, default=0, nullable=False)
    first_seen = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("action", "key", name="uq_auth_throttle_action_key"),
        db.Index("ix_auth_throttle_action_key", "action", "key"),
    )


class UserSecurity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    last_ip = db.Column(db.String(64), nullable=True)
    last_user_agent = db.Column(db.String(255), nullable=True)
    last_fingerprint = db.Column(db.String(64), nullable=True, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ReferralFingerprint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fingerprint = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    referred_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, unique=True)


class ReferralCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    code = db.Column(db.String(32), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ReferralSignup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    referrer_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    referred_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    code_used = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    first_paid_at = db.Column(db.DateTime, nullable=True)
    bonuses_applied_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index("ix_referral_signup_referrer_paid", "referrer_user_id", "first_paid_at"),
        db.Index("ix_referral_signup_referrer_created", "referrer_user_id", "created_at"),
    )


class PaymentIntentPricing(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    intent_token = db.Column(db.String(128), nullable=False, unique=True, index=True)
    coupon_code = db.Column(db.String(64), nullable=True)
    original_amount_usd = db.Column(db.String(32), nullable=False)
    final_amount_usd = db.Column(db.String(32), nullable=False)
    discount_amount_usd = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class UserCouponRedemption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    coupon_code = db.Column(db.String(64), nullable=False, index=True)
    intent_token = db.Column(db.String(128), nullable=True, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "coupon_code", name="uq_user_coupon_redemption_user_coupon"),
    )


class PendingRegistration(db.Model):
    __tablename__ = "pending_registration"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(200), nullable=False)
    lang = db.Column(db.String(2), default="en", nullable=False)
    otp_code_hash = db.Column(db.String(64), nullable=False)
    otp_expires_at = db.Column(db.DateTime, nullable=False, index=True)
    otp_attempts = db.Column(db.Integer, default=0, nullable=False)
    send_count = db.Column(db.Integer, default=1, nullable=False)
    last_sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    referral_code = db.Column(db.String(32), nullable=True)
    referral_fingerprint = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PendingEmailChange(db.Model):
    __tablename__ = "pending_email_change"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    new_email = db.Column(db.String(120), nullable=False, unique=True, index=True)
    otp_code_hash = db.Column(db.String(64), nullable=False)
    otp_expires_at = db.Column(db.DateTime, nullable=False, index=True)
    otp_attempts = db.Column(db.Integer, default=0, nullable=False)
    send_count = db.Column(db.Integer, default=1, nullable=False)
    last_sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PendingPasswordReset(db.Model):
    __tablename__ = "pending_password_reset"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, unique=True, index=True)
    otp_code_hash = db.Column(db.String(64), nullable=False)
    otp_expires_at = db.Column(db.DateTime, nullable=False, index=True)
    otp_attempts = db.Column(db.Integer, default=0, nullable=False)
    send_count = db.Column(db.Integer, default=1, nullable=False)
    last_sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class TelegramAuthChallenge(db.Model):
    __tablename__ = "telegram_auth_challenge"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    purpose = db.Column(db.String(16), nullable=False, default="login", index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    telegram_id = db.Column(db.Integer, nullable=True, index=True)
    approved_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    status_reason = db.Column(db.String(64), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    approved_at = db.Column(db.DateTime, nullable=True)
    consumed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        db.Index("ix_telegram_auth_challenge_purpose_expires", "purpose", "expires_at"),
    )
