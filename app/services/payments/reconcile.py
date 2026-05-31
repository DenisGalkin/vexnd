from __future__ import annotations

from datetime import datetime

from app.core.extensions import db
from app.domain.models import PaymentIntent
from app.services.payments.crystalpay import crystalpay_process_paid_invoice
from app.services.payments.cryptobot import cryptobot_process_paid_invoice
from app.services.payments.heleket import heleket_process_paid
from app.services.payments.platega import platega_process_paid_transaction


def process_payment_intent(intent: PaymentIntent) -> tuple[bool, str]:
    if not intent:
        return False, "intent missing"
    if intent.processed_at:
        return True, "already processed"
    intent.last_checked_at = datetime.utcnow()
    external_id = (intent.external_id or "").strip()
    if intent.provider == "cryptobot":
        processed, msg = cryptobot_process_paid_invoice(external_id, intent.token)
    elif intent.provider == "crystalpay":
        processed, msg = crystalpay_process_paid_invoice(external_id, intent.token)
    elif intent.provider == "platega":
        processed, msg = platega_process_paid_transaction(external_id, intent.token)
    elif intent.provider == "heleket":
        processed, msg = heleket_process_paid(ext=external_id, token=intent.token)
    else:
        return False, "unknown provider"
    if processed:
        intent.status = "success"
    elif (msg or "").startswith("not paid"):
        intent.status = "pending"
    elif "failed" in (msg or "").lower():
        intent.status = "failed"
    if not processed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return processed, msg
