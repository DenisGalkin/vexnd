from __future__ import annotations

from datetime import UTC, datetime

from app.core.extensions import db


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TelegramAccount(db.Model):
    __tablename__ = "telegram_account"

    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    username = db.Column(db.String(64), nullable=True, index=True)
    first_name = db.Column(db.String(128), nullable=True)
    last_name = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, nullable=False)


class BotUserState(db.Model):
    __tablename__ = "bot_user_state"

    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    lang = db.Column(db.String(2), nullable=False, default="ru")
    pending_action = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, nullable=False)


class BotPromoCode(db.Model):
    __tablename__ = "bot_promo_code"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    plan_months = db.Column(db.Integer, nullable=True)
    max_uses = db.Column(db.Integer, nullable=True)
    used_count = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)


class BotPromoRedemption(db.Model):
    __tablename__ = "bot_promo_redemption"

    id = db.Column(db.Integer, primary_key=True)
    promo_id = db.Column(db.Integer, db.ForeignKey("bot_promo_code.id"), nullable=False, index=True)
    telegram_id = db.Column(db.Integer, nullable=False, index=True)
    redeemed_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("promo_id", "telegram_id", name="uq_bot_promo_redemption_once"),
    )
