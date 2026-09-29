"""
Core payment/booking state-transition logic, kept separate from the FastAPI
routing layer so it can be unit tested directly and reused by both the
synchronous "simulate payment" endpoint and the async webhook endpoint.
"""
import random
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.logging_conf import get_logger
from app.models import Booking, BookingStatus, Payment, PaymentStatus, WebhookEvent

logger = get_logger(__name__)


class PaymentError(Exception):
    """Raised for business-rule violations; caller maps this to an HTTP error."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def generate_provider_reference() -> str:
    return f"pay_{uuid.uuid4().hex}"


def initiate_payment(
    db: Session,
    booking: Booking,
    success_rate: float,
    force_outcome: PaymentStatus | None = None,
) -> Payment:
    """
    Simulates calling out to a payment gateway. Creates a Payment record and
    immediately (synchronously) resolves it to SUCCESS/FAILED, updating the
    booking accordingly. In a real system this would instead return a
    "pending" response and the *webhook* would resolve the outcome
    asynchronously -- we support both flows here.
    """
    if booking.status == BookingStatus.CANCELLED:
        raise PaymentError("Cannot pay for a cancelled booking.", 400)
    if booking.status == BookingStatus.CONFIRMED:
        raise PaymentError("This booking has already been paid for.", 409)
    if booking.status == BookingStatus.FAILED:
        # Allow retrying payment after a previous failure by resetting to PENDING.
        booking.status = BookingStatus.PENDING

    payment = Payment(
        booking_id=booking.id,
        provider_reference=generate_provider_reference(),
        amount=booking.amount,
        status=PaymentStatus.PENDING,
    )
    db.add(payment)
    db.flush()

    if force_outcome is not None:
        outcome = force_outcome
    else:
        outcome = PaymentStatus.SUCCESS if random.random() < success_rate else PaymentStatus.FAILED

    payment.status = outcome
    booking.status = BookingStatus.CONFIRMED if outcome == PaymentStatus.SUCCESS else BookingStatus.FAILED

    db.commit()
    db.refresh(payment)
    logger.info(
        "payment_initiated",
        extra={
            "extra_fields": {
                "payment_id": payment.id,
                "booking_id": booking.id,
                "outcome": outcome.value,
            }
        },
    )
    return payment


def process_webhook_event(
    db: Session,
    event_id: str,
    provider_reference: str,
    new_status: PaymentStatus,
    raw_payload: str,
) -> tuple[Payment, bool]:
    """
    Idempotently applies a webhook-delivered status update.

    Returns (payment, was_duplicate). Raises PaymentError for unknown
    provider_reference. Safe under concurrent/duplicate delivery of the
    exact same event_id thanks to the unique constraint on
    webhook_events.event_id: only one delivery ever wins the insert.
    """
    # Fast path: we've already processed this exact event before.
    existing_event = db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).first()
    if existing_event is not None:
        payment = (
            db.query(Payment)
            .filter(Payment.provider_reference == existing_event.provider_reference)
            .first()
        )
        logger.info("webhook_duplicate_ignored", extra={"extra_fields": {"event_id": event_id}})
        return payment, True

    payment = db.query(Payment).filter(Payment.provider_reference == provider_reference).first()
    if payment is None:
        raise PaymentError(f"No payment found for provider_reference={provider_reference}", 404)

    # Record the event first. The unique index on event_id is our
    # concurrency guard: if two identical webhook calls race each other,
    # only one INSERT succeeds and the loser falls back to the "duplicate"
    # branch below instead of double-applying the state change.
    event = WebhookEvent(
        event_id=event_id,
        provider_reference=provider_reference,
        status=new_status.value,
        raw_payload=raw_payload,
    )
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        payment = (
            db.query(Payment).filter(Payment.provider_reference == provider_reference).first()
        )
        logger.info(
            "webhook_race_detected_treated_as_duplicate",
            extra={"extra_fields": {"event_id": event_id}},
        )
        return payment, True

    booking: Booking = payment.booking

    # Only apply the transition if the payment is still in a non-terminal
    # state. This protects against a stale/out-of-order webhook trying to
    # flip an already-resolved payment (e.g. a late FAILED arriving after a
    # SUCCESS was already applied by the synchronous endpoint or an earlier
    # webhook call).
    if payment.status == PaymentStatus.PENDING:
        payment.status = new_status
        if booking.status not in (BookingStatus.CANCELLED,):
            booking.status = (
                BookingStatus.CONFIRMED if new_status == PaymentStatus.SUCCESS else BookingStatus.FAILED
            )
        logger.info(
            "webhook_applied",
            extra={
                "extra_fields": {
                    "event_id": event_id,
                    "payment_id": payment.id,
                    "new_status": new_status.value,
                }
            },
        )
    else:
        logger.info(
            "webhook_ignored_terminal_payment",
            extra={
                "extra_fields": {
                    "event_id": event_id,
                    "payment_id": payment.id,
                    "current_status": payment.status.value,
                }
            },
        )

    db.commit()
    db.refresh(payment)
    return payment, False
