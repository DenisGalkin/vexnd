from __future__ import annotations

import os
import re
from decimal import Decimal, ROUND_HALF_UP

from app.core.extensions import db
from app.domain.models import PaymentIntent, PaymentIntentPricing, UserCouponRedemption
from app.domain.plans import format_usd_amount, plan_details, plan_price_usd, to_decimal_amount
from app.http.helpers import translate


def normalize_coupon_code(raw: str | None) -> str:
    code = re.sub(r"[^A-Za-z0-9_-]+", "", (raw or "").strip().upper())
    return code[:64]


def _parse_plan_months(raw: str | None) -> set[int] | None:
    if not raw:
        return None
    parsed_plans: set[int] = set()
    for item in re.split(r"[,|]", raw):
        item = item.strip()
        if not item:
            continue
        try:
            parsed_plans.add(int(item))
        except Exception:
            pass
    return parsed_plans or None


def _parse_legacy_bot_coupon_configs() -> dict[str, dict]:
    configs: dict[str, dict] = {}
    raw = (os.environ.get("BOT_PROMO_CODES") or "").strip()
    if not raw:
        return configs
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        parts = [p.strip() for p in item.split(":")]
        if len(parts) < 2:
            continue
        code = normalize_coupon_code(parts[0])
        if not code or code in configs:
            continue
        value = parts[1].lower()
        max_uses = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        config = {
            "code": code,
            "kind": "bonus",
            "value": Decimal("0.00"),
            "plans": None,
            "max_uses": max_uses,
            "bot_plan_months": None,
        }
        if value.startswith("plan"):
            config["bot_plan_months"] = int(value.removeprefix("plan") or "1")
        configs[code] = config
    return configs


def _legacy_bot_promo_id(coupon_code: str | None) -> int | None:
    code = normalize_coupon_code(coupon_code)
    if not code:
        return None
    try:
        from app.bot.models import BotPromoCode

        promo = BotPromoCode.query.filter_by(code=code).first()
    except Exception:
        promo = None
    return int(promo.id) if promo else None


def _legacy_bot_coupon_redemptions_count(coupon_code: str | None) -> int:
    promo_id = _legacy_bot_promo_id(coupon_code)
    if not promo_id:
        return 0
    try:
        from app.bot.models import BotPromoRedemption

        return BotPromoRedemption.query.filter_by(promo_id=promo_id).count()
    except Exception:
        return 0


def _legacy_bot_coupon_used_by_user(user_id: int | None, coupon_code: str | None) -> bool:
    if not user_id:
        return False
    promo_id = _legacy_bot_promo_id(coupon_code)
    if not promo_id:
        return False
    try:
        from app.bot.models import BotPromoRedemption, TelegramAccount

        account = TelegramAccount.query.filter_by(user_id=int(user_id)).first()
        if not account:
            return False
        return BotPromoRedemption.query.filter_by(promo_id=promo_id, telegram_id=account.telegram_id).first() is not None
    except Exception:
        return False


def coupon_configs() -> dict[str, dict]:
    configs = _parse_legacy_bot_coupon_configs()
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
        if not parts:
            continue
        kind = parts[0].lower()
        if kind not in ("percent", "fixed", "bonus"):
            continue
        value = Decimal("0.00")
        if kind in ("percent", "fixed"):
            if len(parts) < 2:
                continue
            try:
                value = to_decimal_amount(parts[1])
            except Exception:
                continue
        plan_months = _parse_plan_months(parts[2] if len(parts) >= 3 else None)
        max_uses: int | None = None
        bot_plan_months: int | None = None
        for option in parts[3:]:
            if "=" not in option:
                continue
            key, raw_value = option.split("=", 1)
            key = key.strip().lower()
            raw_value = raw_value.strip()
            if not raw_value:
                continue
            try:
                if key == "max_uses":
                    max_uses = int(raw_value)
                elif key == "bot_plan":
                    bot_plan_months = int(raw_value)
            except Exception:
                continue
        configs[code] = {
            "code": code,
            "kind": kind,
            "value": value,
            "plans": plan_months,
            "max_uses": max_uses,
            "bot_plan_months": bot_plan_months,
        }
    return configs


def coupon_config(coupon_code: str | None) -> dict | None:
    code = normalize_coupon_code(coupon_code)
    if not code:
        return None
    return coupon_configs().get(code)


def coupon_total_redemptions(coupon_code: str | None) -> int:
    code = normalize_coupon_code(coupon_code)
    if not code:
        return 0
    current_count = UserCouponRedemption.query.filter_by(coupon_code=code).count()
    return current_count + _legacy_bot_coupon_redemptions_count(code)


def coupon_exhausted(coupon: dict | None) -> bool:
    if not coupon:
        return False
    max_uses = coupon.get("max_uses")
    if max_uses is None:
        return False
    return coupon_total_redemptions(coupon.get("code")) >= int(max_uses)


def coupon_already_used_by_user(user_id: int | None, coupon_code: str | None) -> bool:
    code = normalize_coupon_code(coupon_code)
    if not user_id or not code:
        return False
    return (
        UserCouponRedemption.query.filter_by(user_id=int(user_id), coupon_code=code).first() is not None
        or _legacy_bot_coupon_used_by_user(user_id, code)
    )


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
    coupon = coupon_config(code)
    if not coupon:
        result["error"] = translate("Промокод не найден.")
        return result
    if coupon_exhausted(coupon):
        result["error"] = translate("Промокод больше недоступен.")
        return result
    if coupon["kind"] not in ("percent", "fixed"):
        result["error"] = translate("Этот промокод нельзя применить к оплате.")
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


def bot_coupon_benefits(coupon_code: str | None, user_id: int | None = None) -> dict:
    code = normalize_coupon_code(coupon_code)
    result = {
        "coupon_code": code or None,
        "coupon_found": False,
        "coupon_applied": False,
        "bot_plan_months": None,
        "error": None,
    }
    if not code:
        result["error"] = "not_found"
        return result
    if coupon_already_used_by_user(user_id, code):
        result["error"] = "already_used"
        return result
    coupon = coupon_config(code)
    if not coupon:
        result["error"] = "not_found"
        return result
    result["coupon_found"] = True
    if coupon_exhausted(coupon):
        result["error"] = "exhausted"
        return result
    bot_plan_months = coupon.get("bot_plan_months")
    if not bot_plan_months:
        result["error"] = "checkout_only"
        return result
    result.update(
        {
            "coupon_applied": True,
            "bot_plan_months": int(bot_plan_months) if bot_plan_months else None,
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


def intent_expected_amounts(intent: PaymentIntent | None) -> set[Decimal]:
    amounts: set[Decimal] = set()
    if not intent:
        return amounts
    meta = PaymentIntentPricing.query.filter_by(intent_token=intent.token).first()
    if meta and meta.final_amount_usd:
        amounts.add(to_decimal_amount(meta.final_amount_usd))
    try:
        amounts.add(to_decimal_amount(plan_price_usd(intent.plan_months)))
    except Exception:
        pass
    try:
        from app.bot.content import BOT_PLAN_CATALOG

        bot_plan = BOT_PLAN_CATALOG.get(int(intent.plan_months))
        if bot_plan and bot_plan.get("price") is not None:
            amounts.add(to_decimal_amount(bot_plan["price"]))
    except Exception:
        pass
    return amounts


def apply_coupon_redemption_for_intent(intent: PaymentIntent | None) -> None:
    if not intent:
        return
    pricing = intent_pricing(intent)
    if not pricing.get("coupon_applied"):
        return
    record_coupon_redemption(intent.user_id, pricing.get("coupon_code"), intent.token)
