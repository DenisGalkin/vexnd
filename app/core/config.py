from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter


SITE_ORIGIN = os.environ.get("SITE_ORIGIN", "https://vexnd.com").rstrip("/")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def build_http_session() -> requests.Session:
    session = requests.Session()
    # Use environment-controlled connection pool and retry settings. Allow a few automatic
    # retries on transient failures to reduce API latency and avoid costly reattempts.
    adapter = HTTPAdapter(
        pool_connections=int(os.environ.get("HTTP_POOL_CONNECTIONS", "20")),
        pool_maxsize=int(os.environ.get("HTTP_POOL_MAXSIZE", "50")),
        max_retries=int(os.environ.get("HTTP_MAX_RETRIES", "3")),
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


HTTP = build_http_session()


def _default_cookie_secure() -> bool:
    try:
        return urlparse(SITE_ORIGIN).scheme.lower() == "https"
    except Exception:
        return False


@dataclass(frozen=True)
class RemnawaveConfig:
    base_url: str
    token: str
    x_api_key: str | None
    internal_squads: tuple[str, ...]


def apply_flask_config(app) -> None:
    provided_secret = (os.environ.get("SECRET_KEY") or "").strip()
    if provided_secret:
        app.config["SECRET_KEY"] = provided_secret
    else:
        app.config["SECRET_KEY"] = secrets.token_urlsafe(48)
        if os.environ.get("FLASK_ENV", "").lower() == "production":
            print("WARNING: SECRET_KEY is not set. Set SECRET_KEY in production to keep sessions stable.")

    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///vexnd.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": int(os.environ.get("SQLALCHEMY_POOL_RECYCLE", "1800")),
    }

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"] = {
            "timeout": int(os.environ.get("SQLITE_BUSY_TIMEOUT", "5")),
        }

    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = int(os.environ.get("SEND_FILE_MAX_AGE_DEFAULT", "31536000"))
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", str(512 * 1024)))
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_NAME"] = os.environ.get("SESSION_COOKIE_NAME", "vexnd_session")
    app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    app.config["SESSION_COOKIE_SECURE"] = _env_bool("SESSION_COOKIE_SECURE", _default_cookie_secure())
    app.config["REMEMBER_COOKIE_HTTPONLY"] = True
    app.config["REMEMBER_COOKIE_NAME"] = os.environ.get("REMEMBER_COOKIE_NAME", "vexnd_remember_v2")
    app.config["REMEMBER_COOKIE_SAMESITE"] = app.config["SESSION_COOKIE_SAMESITE"]
    app.config["REMEMBER_COOKIE_SECURE"] = app.config["SESSION_COOKIE_SECURE"]
    app.config["REMEMBER_COOKIE_REFRESH_EACH_REQUEST"] = False
    app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=int(os.environ.get("REMEMBER_COOKIE_DAYS", "30")))
    app.config["LANGUAGES"] = {"en": "English", "ru": "Русский"}
    app.permanent_session_lifetime = timedelta(days=30)
