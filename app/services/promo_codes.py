from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func

from app.core.extensions import db
from app.domain.models import PaymentIntent, PromoActivation, PromoCode, User


def normalize_promo_code(raw: str | None) -> str:
    code = re.sub(r"[^A-Za-z0-9_-]+", "", (raw or "").strip().upper())
    return code[:64]


def parse_plan_months_csv(raw: str | None) -> set[int] | None:
    if not raw:
        return None
    result: set[int] = set()
    for item in re.split(r"[,|]", raw):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except Exception:
            continue
    return result or None


def decimal_text(value: Decimal | float | int | str | None) -> str | None:
    if value in (None, ""):
        return None
    return format(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def decimal_value(value: Decimal | float | int | str | None) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_db_promo(code: str | None) -> PromoCode | None:
    normalized = normalize_promo_code(code)
    if not normalized:
        return None
    return PromoCode.query.filter_by(code=normalized).first()


def promo_activation_count(promo: PromoCode | None) -> int:
    if not promo:
        return 0
    return int(db.session.query(func.count(PromoActivation.id)).filter(PromoActivation.promo_id == promo.id).scalar() or 0)


def promo_user_activation_count(promo: PromoCode | None, user_id: int | None) -> int:
    if not promo or not user_id:
        return 0
    return int(
        db.session.query(func.count(PromoActivation.id))
        .filter(PromoActivation.promo_id == promo.id, PromoActivation.user_id == int(user_id))
        .scalar()
        or 0
    )


def user_successful_subscription_count(user_id: int | None, *, before: datetime | None = None) -> int:
    if not user_id:
        return 0
    query = db.session.query(func.count(PaymentIntent.id)).filter(
        PaymentIntent.user_id == int(user_id),
        PaymentIntent.plan_months > 0,
        PaymentIntent.status == "success",
    )
    if before is not None:
        query = query.filter(PaymentIntent.processed_at.isnot(None), PaymentIntent.processed_at < before)
    return int(query.scalar() or 0)


@dataclass
class PromoValidationResult:
    ok: bool
    error: str | None = None


def promo_allows_checkout(promo: PromoCode | None) -> bool:
    if not promo:
        return False
    return decimal_value(promo.percent_off) > 0 or decimal_value(promo.fixed_amount_usd) > 0


def promo_has_direct_benefits(promo: PromoCode | None) -> bool:
    if not promo:
        return False
    return int(promo.bonus_days or 0) > 0 or int(promo.bonus_balance_cents or 0) > 0


def validate_promo(
    promo: PromoCode | None,
    *,
    user: User | None = None,
    plan_months: int | None = None,
    for_checkout: bool = False,
    for_direct_activation: bool = False,
    at_time: datetime | None = None,
) -> PromoValidationResult:
    if not promo or not promo.is_active:
        return PromoValidationResult(False, "not_found")
    now = at_time or datetime.utcnow()
    if promo.valid_from and promo.valid_from > now:
        return PromoValidationResult(False, "not_started")
    if promo.valid_until and promo.valid_until < now:
        return PromoValidationResult(False, "expired")
    if promo.max_activations is not None and promo_activation_count(promo) >= int(promo.max_activations):
        return PromoValidationResult(False, "exhausted")
    if user:
        user_count = promo_user_activation_count(promo, user.id)
        per_user_limit = int(promo.max_activations_per_user or 1)
        if per_user_limit > 0 and user_count >= per_user_limit:
            return PromoValidationResult(False, "already_used")
        paid_before = user_successful_subscription_count(user.id, before=now)
        if promo.audience == "new" and paid_before > 0:
            return PromoValidationResult(False, "new_only")
        if promo.audience == "existing" and paid_before == 0:
            return PromoValidationResult(False, "existing_only")
    plan_set = parse_plan_months_csv(promo.plan_months_csv)
    if plan_set and plan_months is not None and int(plan_months) not in plan_set:
        return PromoValidationResult(False, "plan_mismatch")
    if for_checkout and not promo_allows_checkout(promo):
        return PromoValidationResult(False, "checkout_only")
    if for_direct_activation and not promo_has_direct_benefits(promo):
        return PromoValidationResult(False, "payment_only")
    return PromoValidationResult(True, None)


def promo_to_coupon_config(promo: PromoCode) -> dict:
    return {
        "code": promo.code,
        "kind": "db",
        "plans": parse_plan_months_csv(promo.plan_months_csv),
        "max_uses": promo.max_activations,
        "percent_off": decimal_value(promo.percent_off),
        "fixed_amount_usd": decimal_value(promo.fixed_amount_usd),
        "bonus_balance_cents": int(promo.bonus_balance_cents or 0),
        "bonus_days": int(promo.bonus_days or 0),
        "audience": promo.audience,
        "max_activations_per_user": int(promo.max_activations_per_user or 1),
        "promo_id": promo.id,
        "is_active": bool(promo.is_active),
        "valid_from": promo.valid_from,
        "valid_until": promo.valid_until,
    }


def record_promo_activation(
    *,
    promo: PromoCode,
    user_id: int,
    payment_intent_token: str | None = None,
    source: str = "web",
    status: str = "applied",
    discount_amount_usd: Decimal | float | int | str | None = None,
    granted_balance_cents: int = 0,
    granted_days: int = 0,
    notes: str | None = None,
) -> PromoActivation:
    if payment_intent_token:
        existing = PromoActivation.query.filter_by(payment_intent_token=payment_intent_token).first()
        if existing:
            return existing
    activation = PromoActivation(
        promo_id=promo.id,
        user_id=int(user_id),
        payment_intent_token=(payment_intent_token or "").strip() or None,
        source=(source or "web")[:32],
        status=(status or "applied")[:16],
        discount_amount_usd=decimal_text(discount_amount_usd),
        granted_balance_cents=max(0, int(granted_balance_cents or 0)),
        granted_days=max(0, int(granted_days or 0)),
        notes=(notes or "").strip() or None,
        created_at=datetime.utcnow(),
    )
    db.session.add(activation)
    return activation


def apply_direct_promo_code(user: User, code: str | None, *, source: str = "web") -> dict:
    promo = get_db_promo(code)
    validation = validate_promo(promo, user=user, for_direct_activation=True)
    if not validation.ok:
        return {"ok": False, "error": validation.error}

    granted_days = int(promo.bonus_days or 0)
    granted_balance_cents = int(promo.bonus_balance_cents or 0)
    if granted_days <= 0 and granted_balance_cents <= 0:
        return {"ok": False, "error": "payment_only"}

    if granted_balance_cents > 0:
        from app.services.balance import credit_user_balance, format_balance_cents

        credit_user_balance(
            user_id=user.id,
            amount_cents=granted_balance_cents,
            kind="promo_credit",
            description=f"Promo code {promo.code} ({format_balance_cents(granted_balance_cents)})",
        )
    if granted_days > 0:
        from app.services.subscriptions import extend_remnawave_subscription_days

        extend_remnawave_subscription_days(user, granted_days, source="promo", current_plan_months=None)

    record_promo_activation(
        promo=promo,
        user_id=user.id,
        source=source,
        granted_balance_cents=granted_balance_cents,
        granted_days=granted_days,
        notes="direct_activation",
    )
    return {
        "ok": True,
        "promo": promo,
        "granted_days": granted_days,
        "granted_balance_cents": granted_balance_cents,
    }


def apply_paid_promo_effects(
    *,
    user: User,
    code: str | None,
    payment_intent_token: str | None,
    discount_amount_usd: Decimal | float | int | str | None = None,
    source: str = "checkout",
) -> PromoActivation | None:
    promo = get_db_promo(code)
    if not promo:
        return None
    granted_days = int(promo.bonus_days or 0)
    granted_balance_cents = int(promo.bonus_balance_cents or 0)
    if granted_balance_cents > 0:
        from app.services.balance import credit_user_balance, format_balance_cents

        credit_user_balance(
            user_id=user.id,
            amount_cents=granted_balance_cents,
            kind="promo_credit",
            description=f"Promo code {promo.code} ({format_balance_cents(granted_balance_cents)})",
            related_intent_token=payment_intent_token,
        )
    if granted_days > 0:
        from app.services.subscriptions import extend_remnawave_subscription_days

        extend_remnawave_subscription_days(user, granted_days, source="promo", current_plan_months=None)
    return record_promo_activation(
        promo=promo,
        user_id=user.id,
        payment_intent_token=payment_intent_token,
        source=source,
        status="converted",
        discount_amount_usd=discount_amount_usd,
        granted_balance_cents=granted_balance_cents,
        granted_days=granted_days,
        notes="paid_activation",
    )


def promo_conversion_count(promo: PromoCode | None) -> int:
    if not promo:
        return 0
    activations = PromoActivation.query.filter_by(promo_id=promo.id).all()
    total = 0
    for activation in activations:
        has_paid = (
            db.session.query(PaymentIntent.id)
            .filter(
                PaymentIntent.user_id == activation.user_id,
                PaymentIntent.plan_months > 0,
                PaymentIntent.status == "success",
                PaymentIntent.processed_at.isnot(None),
                PaymentIntent.processed_at >= activation.created_at,
            )
            .first()
            is not None
        )
        if has_paid:
            total += 1
    return total
