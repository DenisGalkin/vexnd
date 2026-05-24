from __future__ import annotations

import math
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import abort, current_app, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.core.i18n import translations
from app.core.config import SITE_ORIGIN
from app.core.extensions import db, login_manager
from app.domain.models import Subscription, User, UserSecurity
from app.services.referrals import get_or_create_referral_code, mask_email
from app.services.security import client_ip, csrf_token, device_fingerprint, ensure_db_schema, require_csrf
from app.services.subscriptions import ensure_remnawave_subscription_url


LOCALIZED_PATHS = {
    "/",
    "/dashboard",
    "/checkout",
    "/setup",
    "/faq",
    "/pricing",
    "/soon",
    "/login",
    "/register",
}

LOCALIZED_ENDPOINTS = {
    "index",
    "index_en",
    "dashboard",
    "setup",
    "checkout",
    "faq",
    "faq_page",
    "coming_soon",
    "login",
    "register",
    "tos",
    "tos_en",
    "terms_page",
    "privacy_policy_page",
    "refund_policy_page",
    "aup_page",
    "coupon_preview",
    "pricing",
}


def is_local_http_request() -> bool:
    try:
        host = (request.host or "").split(":", 1)[0].lower()
    except Exception:
        return False
    if host not in {"127.0.0.1", "localhost"}:
        return False
    try:
        xf_proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
        return not request.is_secure and xf_proto != "https"
    except Exception:
        return True


def strip_en_prefix(path: str) -> str:
    if path == "/en":
        return "/"
    if path.startswith("/en/"):
        return path[3:] or "/"
    return path


def public_url(endpoint: str, canonical: bool = False, **values) -> str:
    path = url_for(endpoint, **values)
    if canonical:
        path = strip_en_prefix(path)
    return SITE_ORIGIN + path


def get_locale() -> str:
    if request.path.startswith("/en"):
        return "en"
    lang = session.get("lang")
    if lang:
        return lang
    if current_user.is_authenticated and current_user.lang:
        return current_user.lang
    for locale, _quality in request.accept_languages:
        if locale and (locale.lower().startswith("ru") or locale.lower().startswith("uk")):
            return "ru"
        if locale.lower().startswith("en"):
            return "en"
    return "en"


def translate(text: str) -> str:
    return translations.get(get_locale(), {}).get(text, text)


def localized_url(endpoint: str, **values) -> str:
    url = url_for(endpoint, **values)
    if not url.startswith("/") or url.startswith("/static") or url.startswith("/set_language"):
        return url
    if endpoint not in LOCALIZED_ENDPOINTS:
        return url
    lang = get_locale()
    if lang == "en":
        if url.startswith("/en"):
            return url
        return "/en" if url == "/" else "/en" + url
    return strip_en_prefix(url)


def redirect_localized(endpoint: str, **values):
    return redirect(localized_url(endpoint, **values))


def return_redirect_target() -> str:
    if current_user.is_authenticated:
        return localized_url("dashboard")
    return localized_url("login")


def return_intent_visible(intent) -> bool:
    if not intent:
        return False
    if current_user.is_authenticated and current_user.id != intent.user_id:
        return False
    return True


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def _inject_globals():
    return {"csrf_token": csrf_token}


def _inject_locale():
    return {"get_locale": get_locale}


def _inject_seo_defaults():
    return dict(
        site_origin=SITE_ORIGIN,
        google_site_verification=(os.environ.get("GOOGLE_SITE_VERIFICATION") or "").strip(),
        bing_site_verification=(os.environ.get("BING_SITE_VERIFICATION") or "").strip(),
        default_meta_description=(
            (os.environ.get("META_DESCRIPTION") or "").strip()
            or "VEXND — безопасный VPN с быстрым подключением и удобной оплатой."
        ),
        og_image_url=((os.environ.get("OG_IMAGE_URL") or "").strip() or (SITE_ORIGIN + "/static/images/logo.png")),
    )


def _update_user_security():
    if current_user.is_authenticated:
        session.pop("ref_code", None)
        session.pop("ref_code_set_at", None)
        try:
            now_ts = int(datetime.utcnow().timestamp())
            last_ts = int(session.get("sec_last_update_ts") or 0)
            if now_ts - last_ts < 600:
                return None
            ip = client_ip()
            ua = (request.headers.get("User-Agent", "") or "")[:250]
            fp = device_fingerprint(ip, ua)
            g._sec_update = {"ip": (ip or "")[:63], "ua": ua, "fp": fp, "ts": now_ts}
        except Exception:
            return None
    return None


def _persist_user_security(exc):
    if exc is not None:
        try:
            db.session.rollback()
        except Exception:
            pass
        return None
    if not current_user.is_authenticated:
        return None
    data = getattr(g, "_sec_update", None)
    if not data:
        return None
    try:
        sec = UserSecurity.query.filter_by(user_id=current_user.id).first()
        if not sec:
            sec = UserSecurity(user_id=current_user.id)
            db.session.add(sec)
        sec.last_ip = data["ip"]
        sec.last_user_agent = data["ua"]
        sec.last_fingerprint = data["fp"]
        sec.updated_at = datetime.utcnow()
        db.session.commit()
        session["sec_last_update_ts"] = data["ts"]
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
    return None


def _force_canonical_host_www_to_root():
    try:
        host = (request.host or "").split(":", 1)[0].lower()
    except Exception:
        return None
    if host.startswith("www.") and host.endswith("vexnd.com"):
        target_host = host[4:]
        url = request.url.replace("://www." + target_host, "://" + target_host, 1)
        return redirect(url, code=301)
    return None


def _security_before_request():
    if is_local_http_request():
        current_app.config["SESSION_COOKIE_SECURE"] = False
        current_app.config["REMEMBER_COOKIE_SECURE"] = False
    else:
        configured = current_app.config["SESSION_COOKIE_SECURE"]
        current_app.config["SESSION_COOKIE_SECURE"] = configured
        current_app.config["REMEMBER_COOKIE_SECURE"] = configured
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        exempt = {
            "cryptobot_webhook",
            "cryptobot_webhook_secret",
            "crystalpay_webhook",
            "crystalpay_webhook_secret",
            "platega_webhook",
            "platega_webhook_secret",
            "platega_callback",
            "platega_callback_secret",
            "heleket_webhook",
            "heleket_webhook_secret",
        }
        if (request.endpoint or "") not in exempt:
            require_csrf()


def _security_headers(resp):
    try:
        if request.path.startswith("/static/"):
            resp.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    except Exception:
        pass
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    try:
        xf_proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
        if request.is_secure or xf_proto == "https":
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    except Exception:
        pass
    if not resp.headers.get("Content-Security-Policy"):
        resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; connect-src 'self'; frame-ancestors 'none'"
    return resp


def _bad_request(_error):
    try:
        flash(translate("Неверный запрос. Обновите страницу и попробуйте снова."), "error")
    except Exception:
        pass
    return redirect(request.referrer or url_for("index"))


def _persist_and_canonicalize_language():
    if request.path.startswith("/static"):
        return None
    if "lang" not in session:
        header = (request.headers.get("Accept-Language") or "").lower()
        auto = "ru" if header.startswith("ru") or header.startswith("uk") or " ru" in header or " uk" in header else "en"
        session["lang"] = auto
        session.permanent = True
    lang = session.get("lang", "en")
    path = request.path
    if lang == "en" and not path.startswith("/en") and path in LOCALIZED_PATHS:
        return redirect("/en" if path == "/" else f"/en{path}")
    if lang == "ru" and path.startswith("/en"):
        return redirect(path[3:] or "/")
    return None


def _redirect_to_language_version():
    excluded_paths = [
        "/set_language/",
        "/static/",
        "/login",
        "/register",
        "/logout",
        "/dashboard",
        "/setup",
        "/checkout",
        "/terms",
        "/en/terms",
        "/privacy-policy",
        "/en/privacy-policy",
        "/refund-policy",
        "/en/refund-policy",
        "/aup",
        "/en/aup",
        "/coupon/preview",
        "/en/coupon/preview",
        "/faq",
        "/en/faq",
        "/soon",
        "/en/soon",
        "/en",
        "/cryptobot/return",
        "/crystalpay/return",
        "/crystalpay/webhook",
    ]
    for path in excluded_paths:
        if request.path.startswith(path):
            return None
    if request.path == "/" and not session.get("lang"):
        prefers_ru = False
        for locale, _q in request.accept_languages:
            if locale and (locale.lower().startswith("ru") or locale.lower().startswith("uk")):
                prefers_ru = True
                break
            if locale.lower().startswith("en"):
                break
        if not prefers_ru:
            return redirect(url_for("index_en"))
    return None


def init_app(app) -> None:
    @event.listens_for(Engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout = {int(os.environ.get('SQLITE_BUSY_TIMEOUT_MS', '5000'))}")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()

    @app.teardown_request
    def _db_teardown(exc):
        if exc:
            try:
                db.session.rollback()
            except Exception:
                pass

    app.before_request(_update_user_security)
    app.teardown_request(_persist_user_security)
    app.before_request(_force_canonical_host_www_to_root)
    app.before_request(_security_before_request)
    app.after_request(_security_headers)
    app.errorhandler(400)(_bad_request)
    app.before_request(_persist_and_canonicalize_language)
    app.before_request(_redirect_to_language_version)
    app.context_processor(_inject_globals)
    app.context_processor(_inject_locale)
    app.context_processor(_inject_seo_defaults)
    app.context_processor(lambda: dict(get_locale=get_locale))
    app.jinja_env.globals.update(_=translate)
    from app.domain.plans import plan_duration_label

    app.jinja_env.globals.update(plan_duration_label=plan_duration_label)
    app.jinja_env.globals.update(url_lang=localized_url)
    with app.app_context():
        ensure_db_schema()


def favicon():
    return send_from_directory(Path(current_app.static_folder, "images"), "vexnd.png", mimetype="image/png")


def robots_txt():
    content = """User-agent: *
Allow: /

# Private / auth
Disallow: /dashboard
Disallow: /en/dashboard
Disallow: /login
Disallow: /en/login
Disallow: /register
Disallow: /en/register
Disallow: /logout
Disallow: /account/change-password
Disallow: /account/delete

# API endpoints
Disallow: /api/

# Payment / callbacks (not useful for indexing)
Disallow: /start_payment/
Disallow: /payment_callback
Disallow: /cryptobot/
Disallow: /crystalpay/
Disallow: /platega/

Sitemap: https://vexnd.com/sitemap.xml
"""
    return current_app.response_class(content, mimetype="text/plain; charset=utf-8")


def sitemap_xml():
    from datetime import date

    today = date.today().isoformat()
    pages = [
        ("/", "daily", "1.0"),
        ("/en/", "daily", "1.0"),
        ("/setup", "monthly", "0.7"),
        ("/en/setup", "monthly", "0.7"),
        ("/checkout", "weekly", "0.9"),
        ("/en/checkout", "weekly", "0.9"),
        ("/faq", "monthly", "0.6"),
        ("/en/faq", "monthly", "0.6"),
        ("/soon", "monthly", "0.2"),
        ("/en/soon", "monthly", "0.2"),
    ]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, freq, priority in pages:
        xml.append("<url>")
        xml.append(f"<loc>{SITE_ORIGIN}{path}</loc>")
        xml.append(f"<lastmod>{today}</lastmod>")
        xml.append(f"<changefreq>{freq}</changefreq>")
        xml.append(f"<priority>{priority}</priority>")
        xml.append("</url>")
    xml.append("</urlset>")
    return current_app.response_class("\n".join(xml), mimetype="application/xml; charset=utf-8")
