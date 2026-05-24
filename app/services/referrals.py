from __future__ import annotations

import secrets
import string
from datetime import datetime

from flask import request

from app.core.extensions import db
from app.domain.models import ReferralCode, ReferralSignup, User, UserSecurity
from app.services.security import device_fingerprint
from app.services.subscriptions import extend_remnawave_subscription_days


def generate_referral_code(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(6, int(length))))


def get_or_create_referral_code(user: User) -> str:
    existing = ReferralCode.query.filter_by(user_id=user.id).first()
    if existing and existing.code:
        return existing.code
    for _ in range(10):
        code = generate_referral_code()
        if not ReferralCode.query.filter_by(code=code).first():
            rc = ReferralCode(user_id=user.id, code=code)
            db.session.add(rc)
            db.session.commit()
            return code
    for _ in range(10):
        code = generate_referral_code(16)
        if not ReferralCode.query.filter_by(code=code).first():
            rc = ReferralCode(user_id=user.id, code=code)
            db.session.add(rc)
            db.session.commit()
            return code
    raise RuntimeError("Failed to generate unique referral code")


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email or ""
    name, domain = email.split("@", 1)
    if len(name) <= 1:
        masked = "*"
    elif len(name) == 2:
        masked = name[0] + "*"
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return masked + "@" + domain


def apply_referral_bonus_if_eligible(paying_user: User) -> None:
    try:
        signup = ReferralSignup.query.filter_by(referred_user_id=paying_user.id).first()
        if not signup or signup.bonuses_applied_at is not None:
            return
        referrer = User.query.get(signup.referrer_user_id)
        if not referrer or referrer.id == paying_user.id:
            signup.bonuses_applied_at = datetime.utcnow()
            if signup.first_paid_at is None:
                signup.first_paid_at = datetime.utcnow()
            db.session.commit()
            return
        now = datetime.utcnow()
        if signup.first_paid_at is None:
            signup.first_paid_at = now
        try:
            ip = request.headers.get("X-Forwarded-For", request.remote_addr) or request.remote_addr
            ua = request.headers.get("User-Agent", "")[:250]
            fp = device_fingerprint(ip, ua)
            ref_sec = UserSecurity.query.filter_by(user_id=referrer.id).first()
            if ref_sec and ref_sec.last_fingerprint and fp and ref_sec.last_fingerprint == fp:
                signup.bonuses_applied_at = datetime.utcnow()
                db.session.commit()
                return
        except Exception:
            pass
        extend_remnawave_subscription_days(paying_user, 3)
        extend_remnawave_subscription_days(referrer, 5)
        signup.bonuses_applied_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        print(f"Referral bonus apply failed: {exc}")
        try:
            db.session.rollback()
        except Exception:
            pass
