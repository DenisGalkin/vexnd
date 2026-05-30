from __future__ import annotations

from datetime import datetime
from typing import Callable, Any
from sqlalchemy.exc import IntegrityError


def create_intent_with_pricing(
    *,
    db_session: Any,
    intent_model: Any,
    create_pricing_fn: Callable[[str, dict], Any],
    provider: str,
    token: str,
    user_id: int,
    plan_months: int,
    purpose: str = "subscription",
    balance_amount_cents: int | None = None,
    external_id: str | None,
    pricing: dict,
) -> Any:
    """Create PaymentIntent (+ optional pricing snapshot) in one transaction."""
    intent = intent_model(
        provider=provider,
        token=token,
        user_id=user_id,
        plan_months=plan_months,
        purpose=purpose,
        balance_amount_cents=balance_amount_cents,
        external_id=external_id or None,
    )
    db_session.add(intent)
    intent_pricing = create_pricing_fn(token, pricing)
    if intent_pricing:
        db_session.add(intent_pricing)
    db_session.commit()
    return intent


def mark_processed_payment(
    *,
    db_session: Any,
    processed_model: Any,
    provider: str,
    external_id: str,
    intent: Any,
) -> None:
    """Persist processed marker and mark intent processed atomically."""
    db_session.add(processed_model(provider=provider, external_id=external_id))
    intent.processed_at = getattr(intent, "processed_at", None) or datetime.utcnow()
    try:
        db_session.commit()
    except IntegrityError:
        db_session.rollback()
        # Another worker/process may have finished the same payment first.
        existing = processed_model.query.filter_by(provider=provider, external_id=external_id).first()
        if existing:
            return
        raise
