from __future__ import annotations

import os
import secrets
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import func, inspect, text

from app.core.extensions import db
from app.bot.models import BotTrackedLink, BotTrackedLinkAttribution, BotTrackedLinkPayment, BotTrackedLinkVisit, TelegramAccount, utc_now
from app.domain.models import PaymentIntent, User
from app.services.telegram_links import telegram_bot_deeplink


TRACKED_LINK_PREFIX = "trk_"


def _split_env_list(raw: str | None) -> list[str]:
    value = (raw or "").replace("\n", ",")
    return [item.strip() for item in value.split(",") if item.strip()]


def bot_admin_ids() -> set[int]:
    result: set[int] = set()
    for item in _split_env_list(os.environ.get("BOT_ADMIN_IDS")):
        # Tolerate accidental duplicated assignment fragments like
        # "BOT_ADMIN_IDS=12345,67890" inside the env value itself.
        if "=" in item:
            item = item.rsplit("=", 1)[-1].strip()
        try:
            result.add(int(item))
        except ValueError:
            continue
    return result


def bot_admin_usernames() -> set[str]:
    return {item.lstrip("@").lower() for item in _split_env_list(os.environ.get("BOT_ADMIN_USERNAMES"))}


def is_bot_admin(telegram_id: int | None, username: str | None = None) -> bool:
    if telegram_id is not None and telegram_id in bot_admin_ids():
        return True
    normalized_username = (username or "").strip().lstrip("@").lower()
    return bool(normalized_username) and normalized_username in bot_admin_usernames()


def make_tracked_start_arg(token: str) -> str:
    return f"{TRACKED_LINK_PREFIX}{token}"


def ensure_bot_admin_schema() -> None:
    inspector = inspect(db.engine)
    tracked_link_columns = {column["name"] for column in inspector.get_columns("bot_tracked_link")} if inspector.has_table("bot_tracked_link") else set()
    if tracked_link_columns and "commission_bps" not in tracked_link_columns:
        db.session.execute(text("ALTER TABLE bot_tracked_link ADD COLUMN commission_bps INTEGER NOT NULL DEFAULT 0"))
        db.session.commit()

    if not inspector.has_table("bot_tracked_link_attribution"):
        BotTrackedLinkAttribution.__table__.create(db.engine)
    if not inspector.has_table("bot_tracked_link_payment"):
        BotTrackedLinkPayment.__table__.create(db.engine)


def format_commission_percent(commission_bps: int | None) -> str:
    return f"{Decimal(max(0, int(commission_bps or 0))) / Decimal('100'):.2f}%"


def parse_commission_percent(raw_value: str) -> int:
    normalized = (raw_value or "").strip().replace("%", "").replace(",", ".")
    if not normalized:
        raise ValueError("empty_percent")
    try:
        percent = Decimal(normalized)
    except Exception as exc:
        raise ValueError("invalid_percent") from exc
    if percent < 0 or percent > 100:
        raise ValueError("invalid_percent")
    return int((percent * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _decimal_to_cents(amount: Decimal | float | int | str) -> int:
    return int((Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) * 100)


def create_tracked_link(*, name: str, created_by_telegram_id: int) -> BotTrackedLink:
    clean_name = " ".join((name or "").split()).strip()
    if not clean_name:
        raise ValueError("empty_name")
    if len(clean_name) > 80:
        raise ValueError("name_too_long")

    while True:
        token = secrets.token_urlsafe(8).replace("-", "").replace("_", "")
        if not BotTrackedLink.query.filter_by(token=token).first():
            break

    link = BotTrackedLink(
        token=token,
        name=clean_name,
        created_by_telegram_id=created_by_telegram_id,
        commission_bps=0,
    )
    db.session.add(link)
    db.session.commit()
    return link


def tracked_link_url(link: BotTrackedLink) -> str | None:
    return telegram_bot_deeplink(make_tracked_start_arg(link.token))


def tracked_link_by_start_arg(start_arg: str) -> BotTrackedLink | None:
    if not start_arg.startswith(TRACKED_LINK_PREFIX):
        return None
    token = start_arg.removeprefix(TRACKED_LINK_PREFIX).strip()
    if not token:
        return None
    return BotTrackedLink.query.filter_by(token=token).first()


def register_tracked_link_start(start_arg: str, telegram_id: int, *, is_first_interaction: bool = False) -> BotTrackedLink | None:
    link = tracked_link_by_start_arg(start_arg)
    if not link:
        return None

    now = utc_now()
    link.total_starts += 1
    link.last_started_at = now

    visit = BotTrackedLinkVisit.query.filter_by(link_id=link.id, telegram_id=telegram_id).first()
    if visit:
        visit.starts_count += 1
        visit.last_started_at = now
    else:
        visit = BotTrackedLinkVisit(
            link_id=link.id,
            telegram_id=telegram_id,
            starts_count=1,
            first_started_at=now,
            last_started_at=now,
        )
        link.unique_starts += 1
        db.session.add(visit)
        if is_first_interaction and not BotTrackedLinkAttribution.query.filter_by(telegram_id=telegram_id).first():
            db.session.add(BotTrackedLinkAttribution(link_id=link.id, telegram_id=telegram_id, attributed_at=now))

    db.session.commit()
    return link


def tracked_link_report(limit: int = 20) -> list[dict[str, Any]]:
    items = (
        BotTrackedLink.query.order_by(BotTrackedLink.created_at.desc(), BotTrackedLink.id.desc())
        .limit(max(1, min(int(limit), 100)))
        .all()
    )
    payment_stats = {
        row.link_id: row
        for row in (
            db.session.query(
                BotTrackedLinkPayment.link_id.label("link_id"),
                func.count(BotTrackedLinkPayment.id).label("payments_count"),
                func.count(func.distinct(BotTrackedLinkPayment.user_id)).label("paid_users"),
                func.coalesce(func.sum(BotTrackedLinkPayment.payment_amount_cents), 0).label("paid_amount_cents"),
                func.coalesce(func.sum(BotTrackedLinkPayment.commission_amount_cents), 0).label("commission_amount_cents"),
            )
            .group_by(BotTrackedLinkPayment.link_id)
            .all()
        )
    }
    return [
        {
            "id": item.id,
            "name": item.name,
            "token": item.token,
            "url": tracked_link_url(item),
            "commission_bps": int(item.commission_bps or 0),
            "total_starts": item.total_starts,
            "unique_starts": item.unique_starts,
            "created_at": item.created_at,
            "last_started_at": item.last_started_at,
            "payments_count": int(getattr(payment_stats.get(item.id), "payments_count", 0) or 0),
            "paid_users": int(getattr(payment_stats.get(item.id), "paid_users", 0) or 0),
            "paid_amount_cents": int(getattr(payment_stats.get(item.id), "paid_amount_cents", 0) or 0),
            "commission_amount_cents": int(getattr(payment_stats.get(item.id), "commission_amount_cents", 0) or 0),
        }
        for item in items
    ]


def tracked_link_details(link_id: int) -> dict[str, Any] | None:
    link = db.session.get(BotTrackedLink, int(link_id))
    if not link:
        return None

    attributed_users = (
        db.session.query(func.count(BotTrackedLinkAttribution.id))
        .filter(BotTrackedLinkAttribution.link_id == link.id)
        .scalar()
        or 0
    )
    payment_rows = (
        db.session.query(
            BotTrackedLinkPayment.purpose.label("purpose"),
            func.count(BotTrackedLinkPayment.id).label("count"),
            func.coalesce(func.sum(BotTrackedLinkPayment.payment_amount_cents), 0).label("amount_cents"),
            func.coalesce(func.sum(BotTrackedLinkPayment.commission_amount_cents), 0).label("commission_cents"),
        )
        .filter(BotTrackedLinkPayment.link_id == link.id)
        .group_by(BotTrackedLinkPayment.purpose)
        .all()
    )
    purpose_stats = {
        row.purpose: {
            "count": int(row.count or 0),
            "amount_cents": int(row.amount_cents or 0),
            "commission_cents": int(row.commission_cents or 0),
        }
        for row in payment_rows
    }
    totals = (
        db.session.query(
            func.count(BotTrackedLinkPayment.id).label("payments_count"),
            func.count(func.distinct(BotTrackedLinkPayment.user_id)).label("paid_users"),
            func.coalesce(func.sum(BotTrackedLinkPayment.payment_amount_cents), 0).label("paid_amount_cents"),
            func.coalesce(func.sum(BotTrackedLinkPayment.commission_amount_cents), 0).label("commission_amount_cents"),
            func.max(BotTrackedLinkPayment.paid_at).label("last_paid_at"),
        )
        .filter(BotTrackedLinkPayment.link_id == link.id)
        .one()
    )
    return {
        "id": link.id,
        "name": link.name,
        "token": link.token,
        "url": tracked_link_url(link),
        "commission_bps": int(link.commission_bps or 0),
        "total_starts": int(link.total_starts or 0),
        "unique_starts": int(link.unique_starts or 0),
        "attributed_users": int(attributed_users or 0),
        "payments_count": int(totals.payments_count or 0),
        "paid_users": int(totals.paid_users or 0),
        "paid_amount_cents": int(totals.paid_amount_cents or 0),
        "commission_amount_cents": int(totals.commission_amount_cents or 0),
        "last_paid_at": totals.last_paid_at,
        "created_at": link.created_at,
        "last_started_at": link.last_started_at,
        "subscription_count": int(purpose_stats.get("subscription", {}).get("count", 0)),
        "subscription_amount_cents": int(purpose_stats.get("subscription", {}).get("amount_cents", 0)),
        "subscription_commission_cents": int(purpose_stats.get("subscription", {}).get("commission_cents", 0)),
        "balance_topup_count": int(purpose_stats.get("balance_topup", {}).get("count", 0)),
        "balance_topup_amount_cents": int(purpose_stats.get("balance_topup", {}).get("amount_cents", 0)),
        "balance_topup_commission_cents": int(purpose_stats.get("balance_topup", {}).get("commission_cents", 0)),
    }


def update_tracked_link_commission(link_id: int, commission_bps: int) -> BotTrackedLink | None:
    link = db.session.get(BotTrackedLink, int(link_id))
    if not link:
        return None
    link.commission_bps = max(0, int(commission_bps))
    db.session.commit()
    return link


def record_tracked_link_payment(intent: PaymentIntent, user: User) -> None:
    if not intent or not user or not getattr(intent, "token", None):
        return
    if BotTrackedLinkPayment.query.filter_by(intent_token=intent.token).first():
        return
    account = TelegramAccount.query.filter_by(user_id=user.id).first()
    if not account:
        return
    attribution = BotTrackedLinkAttribution.query.filter_by(telegram_id=account.telegram_id).first()
    if not attribution:
        return
    link = db.session.get(BotTrackedLink, attribution.link_id)
    if not link:
        return
    from app.services.coupons import intent_pricing

    pricing = intent_pricing(intent)
    amount_cents = _decimal_to_cents(pricing["final_price"])
    if amount_cents <= 0:
        return
    commission_bps = max(0, int(link.commission_bps or 0))
    commission_amount_cents = int(
        (Decimal(amount_cents) * Decimal(commission_bps) / Decimal("10000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    db.session.add(
        BotTrackedLinkPayment(
            link_id=link.id,
            telegram_id=account.telegram_id,
            user_id=user.id,
            intent_token=intent.token,
            provider=(intent.provider or "").strip(),
            purpose=(intent.purpose or "subscription").strip(),
            payment_amount_cents=amount_cents,
            commission_bps=commission_bps,
            commission_amount_cents=max(0, commission_amount_cents),
        )
    )
