from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from flask import abort, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user

from app.core.extensions import db
from app.domain.plans import format_usd_amount, plan_catalog, plan_details
from app.services.coupons import coupon_pricing, normalize_coupon_code
from app.http.helpers import favicon_ico, favicon_png, get_locale, robots_txt, sitemap_xml, translate


POLICY_DATE_RU = "30 марта 2026"
POLICY_DATE_EN = "March 30, 2026"
POLICY_SOURCES_DIR = Path(__file__).resolve().parents[2] / "ui" / "templates" / "legal_sources"


def load_policy_parts(filename: str, date_text: str) -> tuple[str, str, str]:
    raw_html = POLICY_SOURCES_DIR.joinpath(filename).read_text(encoding="utf-8").strip().replace("[DATE]", date_text)
    title_match = re.search(r"<h1>(.*?)</h1>\s*", raw_html, re.DOTALL)
    if not title_match:
        raise RuntimeError(f"Policy file {filename} is missing an <h1> header")
    title = title_match.group(1).strip()
    rest = raw_html[title_match.end() :].lstrip()
    updated_match = re.search(r"<p>(.*?)</p>\s*", rest, re.DOTALL)
    if not updated_match:
        raise RuntimeError(f"Policy file {filename} is missing the updated date paragraph")
    updated_line = updated_match.group(1).strip()
    body_html = rest[updated_match.end() :].strip()
    body_html = re.sub(r"\s*<h2>\d+\.\s*(?:Контакты|Contact|Reporting)</h2>\s*(?:<p>.*?</p>\s*)+$", "", body_html, flags=re.DOTALL).strip()
    return title, updated_line, body_html


def render_policy_page(template_name: str, ru_filename: str, en_filename: str):
    ru_title, ru_updated, ru_body = load_policy_parts(ru_filename, POLICY_DATE_RU)
    en_title, en_updated, en_body = load_policy_parts(en_filename, POLICY_DATE_EN)
    page_title = translate(ru_title)
    updated_line = translate(ru_updated)
    page_content = translate(ru_body)
    if get_locale() == "en":
        if page_title == ru_title:
            page_title = en_title
        if updated_line == ru_updated:
            updated_line = en_updated
        if page_content == ru_body:
            page_content = en_body
    return render_template(template_name, page_title=page_title, updated_line=updated_line, page_content=page_content)


def index():
    return render_template("index.html", plans=_pricing_plans())


def index_en():
    return index()


def _pricing_plans() -> list[dict[str, object]]:
    return [
        {
            "months": plan["months"],
            "price": format_usd_amount(plan["price"]),
            "features": plan["features"],
        }
        for plan in plan_catalog().values()
    ]


def pricing():
    return render_template("pricing.html", plans=_pricing_plans())


def set_language(lang):
    if lang in ["ru", "en"]:
        session["lang"] = lang
        session.permanent = True
        if current_user.is_authenticated:
            current_user.lang = lang
            db.session.commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"status": "success", "lang": lang})
    current_path = (request.args.get("next", "") or "").strip()
    if current_path and (not current_path.startswith("/") or current_path.startswith("//")):
        current_path = ""
    if current_path:
        if lang == "en" and not current_path.startswith("/en"):
            return redirect("/en" + current_path)
        if lang == "ru" and current_path.startswith("/en"):
            return redirect(current_path[3:] or "/")
        return redirect(current_path)
    return redirect(url_for("index_en" if lang == "en" else "index"))


def setup():
    happ_ios_url = "https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973" if get_locale() == "ru" else "https://apps.apple.com/app/happ-proxy-utility/id6504287215"
    clients = {
        "ios": [
            {"name": "v2RayTun", "url": "https://apps.apple.com/app/v2raytun/id6476628951", "store": "App Store"},
            {"name": "Happ", "url": happ_ios_url, "store": "App Store"},
        ],
        "android": [
            {"name": "v2RayTun", "url": "https://play.google.com/store/apps/details?id=com.v2raytun.android", "store": "Google Play"},
            {"name": "FlClashX", "url": "https://github.com/pluralplay/FlClashX/releases/download/v0.3.2/FlClashX-android-universal.apk", "store": "GitHub"},
            {"name": "Happ", "url": "https://play.google.com/store/apps/details?id=com.happproxy&pcampaignid=web_share", "store": "Google Play"},
        ],
        "windows": [
            {"name": "FlClashX", "url": "https://github.com/pluralplay/FlClashX/releases/latest/download/FlClashX-windows-amd64-setup.exe", "store": "GitHub"},
            {"name": "v2RayTun", "url": "https://storage.v2raytun.com/v2RayTun_Setup.exe", "store": "Website"},
            {"name": "Happ", "url": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe", "store": "GitHub"},
        ],
        "macos": [
            {"name": "FlClashX", "url": "https://github.com/pluralplay/FlClashX/releases/download/v0.3.2/FlClashX-macos-arm64.dmg", "store": "GitHub"},
            {"name": "v2RayTun", "url": "https://apps.apple.com/app/v2raytun/id6476628951", "store": "App Store"},
            {"name": "Happ", "url": happ_ios_url, "store": "App Store"},
        ],
    }
    return render_template("setup.html", clients=clients)


def open_app():
    target = (request.args.get("target") or "").strip()
    fallback = (request.args.get("fallback") or "").strip()
    allowed_prefixes = ("happ://", "v2raytun://", "v2rayng://", "flclashx://")
    if not target or not any(target.startswith(prefix) for prefix in allowed_prefixes):
        abort(400)
    if fallback and not (fallback.startswith("https://") or fallback.startswith("http://")):
        fallback = ""
    return render_template("open_app.html", target=target, fallback=fallback)


def terms_page():
    return render_policy_page("tos.html", "tos.ru.html", "tos.en.html")


def privacy_policy_page():
    return render_policy_page("privacy_policy.html", "privacy_policy.ru.html", "privacy_policy.en.html")


def refund_policy_page():
    return render_policy_page("refund_policy.html", "refund_policy.ru.html", "refund_policy.en.html")


def aup_page():
    return render_policy_page("aup.html", "aup.ru.html", "aup.en.html")


def faq_page():
    return render_template("faq.html")


def coming_soon():
    return render_template("coming_soon.html")


def register(app) -> None:
    app.add_url_rule("/", endpoint="index", view_func=index, methods=["GET"])
    app.add_url_rule("/en", endpoint="index_en", view_func=index_en, methods=["GET"])
    app.add_url_rule("/en/", endpoint="index_en_slash", view_func=index_en, methods=["GET"])
    app.add_url_rule("/pricing", endpoint="pricing", view_func=pricing, methods=["GET"])
    app.add_url_rule("/en/pricing", endpoint="pricing_en", view_func=pricing, methods=["GET"])
    app.add_url_rule("/set_language/<lang>", endpoint="set_language", view_func=set_language, methods=["GET", "POST"])
    app.add_url_rule("/setup", endpoint="setup", view_func=setup, methods=["GET"])
    app.add_url_rule("/en/setup", endpoint="setup_en", view_func=setup, methods=["GET"])
    app.add_url_rule("/open-app", endpoint="open_app", view_func=open_app, methods=["GET"])
    app.add_url_rule("/en/open-app", endpoint="open_app_en", view_func=open_app, methods=["GET"])
    app.add_url_rule("/terms", endpoint="terms_page", view_func=terms_page, methods=["GET"])
    app.add_url_rule("/en/terms", endpoint="terms_page_en", view_func=terms_page, methods=["GET"])
    app.add_url_rule("/privacy-policy", endpoint="privacy_policy_page", view_func=privacy_policy_page, methods=["GET"])
    app.add_url_rule("/en/privacy-policy", endpoint="privacy_policy_page_en", view_func=privacy_policy_page, methods=["GET"])
    app.add_url_rule("/refund-policy", endpoint="refund_policy_page", view_func=refund_policy_page, methods=["GET"])
    app.add_url_rule("/en/refund-policy", endpoint="refund_policy_page_en", view_func=refund_policy_page, methods=["GET"])
    app.add_url_rule("/aup", endpoint="aup_page", view_func=aup_page, methods=["GET"])
    app.add_url_rule("/en/aup", endpoint="aup_page_en", view_func=aup_page, methods=["GET"])
    app.add_url_rule("/faq", endpoint="faq_page", view_func=faq_page, methods=["GET"])
    app.add_url_rule("/en/faq", endpoint="faq_page_en", view_func=faq_page, methods=["GET"])
    app.add_url_rule("/soon", endpoint="coming_soon", view_func=coming_soon, methods=["GET"])
    app.add_url_rule("/en/soon", endpoint="coming_soon_en", view_func=coming_soon, methods=["GET"])
    app.add_url_rule("/favicon.ico", endpoint="favicon", view_func=favicon_ico, methods=["GET"])
    app.add_url_rule("/favicon.png", endpoint="favicon_png", view_func=favicon_png, methods=["GET"])
    app.add_url_rule("/robots.txt", endpoint="robots_txt", view_func=robots_txt, methods=["GET"])
    app.add_url_rule("/sitemap.xml", endpoint="sitemap_xml", view_func=sitemap_xml, methods=["GET"])
