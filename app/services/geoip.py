from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass

from app.core.config import HTTP


_CACHE_TTL_SECONDS = 60 * 60 * 12
_CACHE_LOCK = threading.Lock()
_GEOIP_CACHE: dict[tuple[str, str], tuple[float, "GeoIpLocation"]] = {}


@dataclass(frozen=True)
class GeoIpLocation:
    city: str | None
    region: str | None
    country: str | None
    label: str | None


def _normalize_ip(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def _is_public_ip(value: str | None) -> bool:
    normalized = _normalize_ip(value)
    if not normalized:
        return False
    ip_obj = ipaddress.ip_address(normalized)
    return not (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _build_label(city: str | None, region: str | None, country: str | None) -> str | None:
    city = (city or "").strip() or None
    region = (region or "").strip() or None
    country = (country or "").strip() or None
    if city:
        return city
    if region and country:
        return f"{region}, {country}"
    return region or country


def lookup_ip_location(ip: str | None, *, locale: str = "en") -> GeoIpLocation | None:
    normalized_ip = _normalize_ip(ip)
    if not normalized_ip:
        return None
    if not _is_public_ip(normalized_ip):
        return GeoIpLocation(city=None, region=None, country=None, label="Local network")

    normalized_locale = (locale or "en").strip().lower() or "en"
    cache_key = (normalized_ip, normalized_locale)
    now = time.time()

    with _CACHE_LOCK:
        cached = _GEOIP_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

    try:
        response = HTTP.get(
            f"https://ipwho.is/{normalized_ip}",
            params={"fields": "success,city,region,country", "lang": normalized_locale},
            timeout=2.5,
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
    except Exception:
        return None

    if not isinstance(payload, dict) or payload.get("success") is False:
        return None

    location = GeoIpLocation(
        city=(payload.get("city") or "").strip() or None,
        region=(payload.get("region") or "").strip() or None,
        country=(payload.get("country") or "").strip() or None,
        label=_build_label(payload.get("city"), payload.get("region"), payload.get("country")),
    )

    with _CACHE_LOCK:
        _GEOIP_CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, location)

    return location


__all__ = ["GeoIpLocation", "lookup_ip_location"]
