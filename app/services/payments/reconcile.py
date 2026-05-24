from __future__ import annotations

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
    external_id = (intent.external_id or "").strip()
    if intent.provider == "cryptobot":
        return cryptobot_process_paid_invoice(external_id, intent.token)
    if intent.provider == "crystalpay":
        return crystalpay_process_paid_invoice(external_id, intent.token)
    if intent.provider == "platega":
        return platega_process_paid_transaction(external_id, intent.token)
    if intent.provider == "heleket":
        return heleket_process_paid(ext=external_id, token=intent.token)
    return False, "unknown provider"
