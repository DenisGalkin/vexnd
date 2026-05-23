from __future__ import annotations

import os
import re
from decimal import Decimal, ROUND_HALF_UP

from vexnd_app.extensions import db
from vexnd_app.models import PaymentIntent, PaymentIntentPricing, UserCouponRedemption
from vexnd_app.plans import format_usd_amount, plan_details, plan_price_usd, to_decimal_amount
from vexnd_app.web import translate


def normalize_coupon_code(raw: str | None) -> str:
    code = re.sub(r"[^A-Za-z0-9_-]+", "", (raw or "").strip().upper())
    return code[:64]


def coupon_configs() -> dict[str, dict]:
    configs: dict[str, dict] = {}
    for env_name, env_value in os.environ.items():
        if not env_name.startswith("COUPON_"):
            continue
        code = normalize_coupon_code(env_name[len("COUPON_") :])
        if not code:
            continue
        raw = (env_value or "").strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split(":")]
        if len(parts) < 2:
            continue
        kind = parts[0].lower()
        if kind not in ("percent", "fixed"):
            continue
        try:
            value = to_decimal_amount(parts[1])
        except Exception:
            continue
        plan_months: set[int] | None = None
        if len(parts) >= 3 and parts[2]:
            parsed_plans: set[int] = set()
            for item in re.split(r"[,|]", parts[2]):
                item = item.strip()
                if not item:
                    continue
                try:
                    parsed_plans.add(int(item))
                except Exception:
                    pass
            if parsed_plans:
                plan_months = parsed_plans
        configs[code] = {"code": code, "kind": kind, "value": value, "plans": plan_months}
    return configs


def coupon_already_used_by_user(user_id: int | None, coupon_code: str | None) -> bool:
    code = normalize_coupon_code(coupon_code)
    if not user_id or not code:
        return False
    return UserCouponRedemption.query.filter_by(user_id=int(user_id), coupon_code=code).first() is not None


def record_coupon_redemption(user_id: int | None, coupon_code: str | None, intent_token: str | None = None) -> None:
    code = normalize_coupon_code(coupon_code)
    if not user_id or not code:
        return
    existing = UserCouponRedemption.query.filter_by(user_id=int(user_id), coupon_code=code).first()
    if existing:
        return
    db.session.add(UserCouponRedemption(user_id=int(user_id), coupon_code=code, intent_token=(intent_token or "").strip() or None))


def coupon_pricing(plan_months: int, coupon_code: str | None = None, user_id: int | None = None) -> dict:
    plan = plan_details(plan_months)
    original_price = to_decimal_amount(plan["price"])
    result = {
        "plan_months": plan["months"],
        "coupon_code": None,
        "coupon_applied": False,
        "original_price": original_price,
        "final_price": original_price,
        "discount_amount": Decimal("0.00"),
        "error": None,
    }
    code = normalize_coupon_code(coupon_code)
    if not code:
        return result
    if coupon_already_used_by_user(user_id, code):
        result["error"] = translate("Этот промокод уже использован на вашем аккаунте.")
        return result
    coupon = coupon_configs().get(code)
    if not coupon:
        result["error"] = translate("Промокод не найден.")
        return result
    if coupon["plans"] and plan["months"] not in coupon["plans"]:
        result["error"] = translate("Этот промокод не подходит для выбранного тарифа.")
        return result
    if coupon["kind"] == "percent":
        discount_amount = (original_price * coupon["value"] / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        discount_amount = to_decimal_amount(coupon["value"])
    if discount_amount <= Decimal("0.00"):
        result["error"] = translate("Размер скидки по промокоду некорректен.")
        return result
    max_discount = max(original_price - Decimal("0.01"), Decimal("0.00"))
    discount_amount = min(discount_amount, max_discount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    final_price = (original_price - discount_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if final_price >= original_price:
        result["error"] = translate("Промокод не даёт скидку для этого тарифа.")
        return result
    result.update(
        {
            "coupon_code": code,
            "coupon_applied": True,
            "final_price": final_price,
            "discount_amount": discount_amount,
        }
    )
    return result


def create_intent_pricing(token: str, pricing: dict) -> PaymentIntentPricing | None:
    if not pricing.get("coupon_applied"):
        return None
    return PaymentIntentPricing(
        intent_token=token,
        coupon_code=pricing.get("coupon_code") or None,
        original_amount_usd=format_usd_amount(pricing["original_price"]),
        final_amount_usd=format_usd_amount(pricing["final_price"]),
        discount_amount_usd=format_usd_amount(pricing["discount_amount"]),
    )


def intent_pricing(intent: PaymentIntent | None) -> dict:
    fallback_price = plan_price_usd(intent.plan_months if intent else 1)
    base = {
        "coupon_code": None,
        "coupon_applied": False,
        "original_price": to_decimal_amount(fallback_price),
        "final_price": to_decimal_amount(fallback_price),
        "discount_amount": Decimal("0.00"),
    }
    if not intent:
        return base
    meta = PaymentIntentPricing.query.filter_by(intent_token=intent.token).first()
    if not meta:
        return base
    return {
        "coupon_code": meta.coupon_code or None,
        "coupon_applied": bool(meta.coupon_code),
        "original_price": to_decimal_amount(meta.original_amount_usd),
        "final_price": to_decimal_amount(meta.final_amount_usd),
        "discount_amount": to_decimal_amount(meta.discount_amount_usd),
    }


def apply_coupon_redemption_for_intent(intent: PaymentIntent | None) -> None:
    if not intent:
        return
    pricing = intent_pricing(intent)
    if not pricing.get("coupon_applied"):
        return
    record_coupon_redemption(intent.user_id, pricing.get("coupon_code"), intent.token)
