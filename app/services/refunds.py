"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents.
"""
from datetime import datetime

from ..models import Booking, RefundLog


def calculate_refund_amount(price_cents: int, percent: int) -> int:
    return (price_cents * percent + 50) // 100


def log_refund(db, booking: Booking, amount_cents: int) -> RefundLog:
    entry = RefundLog(
        booking_id=booking.id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=datetime.utcnow(),
    )
    db.add(entry)
    return entry
