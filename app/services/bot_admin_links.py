from __future__ import annotations

import os
import secrets
from typing import Any

from app.core.extensions import db
from app.bot.models import BotTrackedLink, BotTrackedLinkVisit, utc_now
from app.services.telegram_links import telegram_bot_deeplink


TRACKED_LINK_PREFIX = "trk_"


def _split_env_list(raw: str | None) -> list[str]:
    value = (raw or "").replace("\n", ",")
    return [item.strip() for item in value.split(",") if item.strip()]


def bot_admin_ids() -> set[int]:
    result: set[int] = set()
    for item in _split_env_list(os.environ.get("BOT_ADMIN_IDS")):
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


def register_tracked_link_start(start_arg: str, telegram_id: int) -> BotTrackedLink | None:
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

    db.session.commit()
    return link


def tracked_link_report(limit: int = 20) -> list[dict[str, Any]]:
    items = (
        BotTrackedLink.query.order_by(BotTrackedLink.created_at.desc(), BotTrackedLink.id.desc())
        .limit(max(1, min(int(limit), 100)))
        .all()
    )
    return [
        {
            "name": item.name,
            "token": item.token,
            "url": tracked_link_url(item),
            "total_starts": item.total_starts,
            "unique_starts": item.unique_starts,
            "created_at": item.created_at,
            "last_started_at": item.last_started_at,
        }
        for item in items
    ]
