from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def plan_catalog() -> dict[int, dict]:
    return {
        1: {
            "months": 1,
            "price": Decimal("3.99"),
            "features": [
                "Доступ ко всем серверам",
                "Скорость до 1 Гбит/с",
                "Поддержка 24/7",
                "2 ТБ трафика",
                "Экономия 0%",
            ],
        },
        3: {
            "months": 3,
            "price": Decimal("10.99"),
            "features": [
                "Доступ ко всем серверам",
                "Скорость до 1 Гбит/с",
                "Поддержка 24/7",
                "2 ТБ трафика",
                "Экономия 8%",
            ],
        },
        12: {
            "months": 12,
            "price": Decimal("34.99"),
            "features": [
                "Доступ ко всем серверам",
                "Скорость до 1 Гбит/с",
                "Поддержка 24/7",
                "2 ТБ трафика",
                "Экономия 27%",
            ],
        },
    }


def plan_details(plan_months: int) -> dict:
    try:
        plan_months = int(plan_months)
    except Exception:
        plan_months = 1
    plans = plan_catalog()
    return plans.get(plan_months, plans[1])


def plan_price_usd(plan_months: int) -> Decimal:
    return plan_details(plan_months)["price"]


def format_usd_amount(amount: Decimal | float | int | str) -> str:
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(value, "f")


def to_decimal_amount(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def plan_duration_label(plan_months: int, locale: str | None = None) -> str:
    from app.http.helpers import get_locale

    try:
        months = int(plan_months)
    except Exception:
        months = 1
    lang = locale or get_locale()
    if lang == "en":
        unit = "month" if months == 1 else "months"
        return f"{months} {unit}"
    rem10 = months % 10
    rem100 = months % 100
    if rem10 == 1 and rem100 != 11:
        unit = "месяц"
    elif rem10 in (2, 3, 4) and rem100 not in (12, 13, 14):
        unit = "месяца"
    else:
        unit = "месяцев"
    return f"{months} {unit}"
