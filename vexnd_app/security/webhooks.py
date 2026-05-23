from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None


def secrets_match(actual: str | None, expected: str | None) -> bool:
    """Constant-time secret comparison with empty-value guard."""
    if not actual or not expected:
        return False
    return hmac.compare_digest(actual, expected)


def intent_not_expired(intent: Any, *, hours: int = 24) -> bool:
    """Return True when intent exists and is within the allowed age window."""
    if not intent:
        return False
    created_at = getattr(intent, "created_at", None)
    if not created_at:
        return True
    return created_at >= (datetime.utcnow() - timedelta(hours=hours))


def provider_error_id(provider: str, exc: Exception) -> str:
    """Generate non-sensitive short error id for logs and user-facing messages."""
    base = f"{provider}:{exc.__class__.__name__}:{str(exc)[:120]}"
    return hmac.new(b"vexnd-error", base.encode("utf-8"), "sha256").hexdigest()[:10]


def payment_lock_name(provider: str, external_id: str) -> str:
    """Build a stable filesystem-safe lock name for payment processing."""
    raw = f"{provider}:{external_id}".encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()[:40] + ".lock"


@contextmanager
def payment_processing_lock(provider: str, external_id: str):
    """Serialize duplicate callback processing across workers on one host."""
    if not provider or not external_id or fcntl is None:
        yield
        return

    lock_dir = os.environ.get("PAYMENT_LOCK_DIR", os.path.join(tempfile.gettempdir(), "vexnd-payment-locks"))
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, payment_lock_name(provider, external_id))
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
