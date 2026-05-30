from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from app.core.extensions import db
from app.domain.models import BalanceTransaction, PaymentIntent, User, UserBalance
from app.domain.plans import plan_duration_label
from app.services.coupons import apply_coupon_redemption_for_intent, coupon_pricing, intent_pricing
from app.services.referrals import apply_referral_bonus_if_eligible
from app.services.subscriptions import create_remnawave_subscription


MIN_TOPUP_CENTS = 300
MAX_TOPUP_CENTS = 50000
TOPUP_PRESET_CENTS = (500, 1000, 2500, 5000)


def amount_to_cents(amount: Decimal | float | int | str) -> int:
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(value * 100)


def cents_to_decimal(amount_cents: int | None) -> Decimal:
    return (Decimal(max(0, int(amount_cents or 0))) / Decimal("100")).quantize(Decimal("0.01"))


def format_balance_cents(amount_cents: int | None) -> str:
    return f"${cents_to_decimal(amount_cents):.2f}"


def clamp_topup_cents(amount_cents: int) -> int:
    return max(MIN_TOPUP_CENTS, min(MAX_TOPUP_CENTS, int(amount_cents)))


def get_or_create_user_balance(user_id: int) -> UserBalance:
    balance = db.session.get(UserBalance, int(user_id))
    if balance:
        return balance
    balance = UserBalance(user_id=int(user_id), amount_cents=0, updated_at=datetime.utcnow())
    db.session.add(balance)
    db.session.flush()
    return balance


def user_balance_cents(user_id: int) -> int:
    return int(get_or_create_user_balance(int(user_id)).amount_cents or 0)


def create_balance_transaction(
    *,
    user_id: int,
    direction: str,
    kind: str,
    amount_cents: int,
    balance_after_cents: int,
    description: str | None = None,
    related_intent_token: str | None = None,
) -> BalanceTransaction:
    entry = BalanceTransaction(
        user_id=int(user_id),
        direction=(direction or "").strip().lower(),
        kind=(kind or "").strip().lower(),
        amount_cents=max(0, int(amount_cents)),
        balance_after_cents=max(0, int(balance_after_cents)),
        description=(description or "").strip() or None,
        related_intent_token=(related_intent_token or "").strip() or None,
        created_at=datetime.utcnow(),
    )
    db.session.add(entry)
    return entry


def credit_user_balance(
    *,
    user_id: int,
    amount_cents: int,
    kind: str,
    description: str | None = None,
    related_intent_token: str | None = None,
) -> UserBalance:
    amount_cents = max(0, int(amount_cents))
    if amount_cents <= 0:
        raise ValueError("amount_must_be_positive")
    balance = get_or_create_user_balance(int(user_id))
    balance.amount_cents = int(balance.amount_cents or 0) + amount_cents
    balance.updated_at = datetime.utcnow()
    create_balance_transaction(
        user_id=user_id,
        direction="credit",
        kind=kind,
        amount_cents=amount_cents,
        balance_after_cents=balance.amount_cents,
        description=description,
        related_intent_token=related_intent_token,
    )
    return balance


def debit_user_balance(
    *,
    user_id: int,
    amount_cents: int,
    kind: str,
    description: str | None = None,
    related_intent_token: str | None = None,
) -> UserBalance:
    amount_cents = max(0, int(amount_cents))
    if amount_cents <= 0:
        raise ValueError("amount_must_be_positive")
    balance = get_or_create_user_balance(int(user_id))
    current = int(balance.amount_cents or 0)
    if current < amount_cents:
        raise ValueError("insufficient_balance")
    balance.amount_cents = current - amount_cents
    balance.updated_at = datetime.utcnow()
    create_balance_transaction(
        user_id=user_id,
        direction="debit",
        kind=kind,
        amount_cents=amount_cents,
        balance_after_cents=balance.amount_cents,
        description=description,
        related_intent_token=related_intent_token,
    )
    return balance


def topup_description(amount_cents: int) -> str:
    return f"VEXND balance top-up ({format_balance_cents(amount_cents)})"


def subscription_balance_description(plan_months: int) -> str:
    return f"VEXND subscription via balance ({plan_duration_label(plan_months, 'en')})"


def can_pay_for_plan_with_balance(user_id: int, plan_months: int, coupon_code: str | None = None) -> tuple[bool, dict]:
    pricing = coupon_pricing(plan_months, coupon_code, int(user_id))
    needed_cents = amount_to_cents(pricing["final_price"])
    return user_balance_cents(int(user_id)) >= needed_cents, pricing


def purchase_subscription_with_balance(user: User, plan_months: int, coupon_code: str | None = None) -> dict:
    pricing = coupon_pricing(plan_months, coupon_code, user.id)
    if pricing.get("error"):
        raise ValueError(str(pricing["error"]))
    amount_cents = amount_to_cents(pricing["final_price"])
    try:
        debit_user_balance(
            user_id=user.id,
            amount_cents=amount_cents,
            kind="subscription_purchase",
            description=subscription_balance_description(plan_months),
        )
    except ValueError as exc:
        if str(exc) == "insufficient_balance":
            raise ValueError("insufficient_balance") from exc
        raise
    create_remnawave_subscription(user, int(plan_months), strict=True)
    apply_referral_bonus_if_eligible(user)
    db.session.commit()
    return pricing


def fulfill_payment_intent(intent: PaymentIntent, user: User, external_id: str) -> None:
    purpose = (getattr(intent, "purpose", None) or "subscription").strip().lower()
    if purpose == "balance_topup":
        amount_cents = int(getattr(intent, "balance_amount_cents", 0) or 0)
        if amount_cents <= 0:
            pricing = intent_pricing(intent)
            amount_cents = amount_to_cents(pricing["final_price"])
        credit_user_balance(
            user_id=user.id,
            amount_cents=amount_cents,
            kind="topup",
            description=topup_description(amount_cents),
            related_intent_token=intent.token,
        )
        return
    create_remnawave_subscription(user, int(intent.plan_months), strict=True)
    apply_coupon_redemption_for_intent(intent)
    apply_referral_bonus_if_eligible(user)
