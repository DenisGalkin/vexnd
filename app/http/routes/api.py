from __future__ import annotations

from flask import jsonify, request
from flask_login import current_user, login_required

from app.domain.plans import format_usd_amount, plan_duration_label
from app.services.coupons import coupon_pricing
from app.services.security import require_csrf
from app.http.helpers import translate


@login_required
def coupon_preview():
    require_csrf()
    payload = request.get_json(silent=True) if request.is_json else {}
    try:
        plan_months = int(request.form.get("plan") or (payload or {}).get("plan") or 1)
    except Exception:
        plan_months = 1
    coupon_code = request.form.get("coupon_code")
    if coupon_code is None:
        coupon_code = (payload or {}).get("coupon_code")
    pricing = coupon_pricing(plan_months, coupon_code, current_user.id if current_user.is_authenticated else None)
    if pricing.get("error"):
        return jsonify({"ok": False, "error": pricing["error"]}), 400
    return jsonify(
        {
            "ok": True,
            "coupon_code": pricing["coupon_code"] or "",
            "coupon_applied": pricing["coupon_applied"],
            "original_price": format_usd_amount(pricing["original_price"]),
            "final_price": format_usd_amount(pricing["final_price"]),
            "discount_amount": format_usd_amount(pricing["discount_amount"]),
            "plan_label": plan_duration_label(pricing["plan_months"]),
            "message": translate("Промокод применён."),
        }
    )


def filter_payments():
    return jsonify({"filter": request.args.get("type", "all")})


def register(app) -> None:
    app.add_url_rule("/coupon/preview", endpoint="coupon_preview", view_func=coupon_preview, methods=["POST"])
    app.add_url_rule("/en/coupon/preview", endpoint="coupon_preview_en", view_func=coupon_preview, methods=["POST"])
    app.add_url_rule("/api/filter-payments", endpoint="filter_payments", view_func=filter_payments, methods=["GET"])
