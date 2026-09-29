from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.logging_conf import get_logger
from app.models import Booking, User
from app.schemas import PaymentInitiate, PaymentOut, WebhookAck, WebhookPayload
from app.services.payment_service import PaymentError, initiate_payment, process_webhook_event

router = APIRouter(prefix="/payments", tags=["Payments"])
logger = get_logger(__name__)


@router.post("/", response_model=PaymentOut, status_code=status.HTTP_201_CREATED)
def simulate_payment(
    payload: PaymentInitiate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    booking = db.query(Booking).filter(Booking.id == payload.booking_id).first()
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found.")
    if booking.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found.")

    try:
        payment = initiate_payment(
            db,
            booking,
            success_rate=settings.PAYMENT_SUCCESS_RATE,
            force_outcome=payload.force_outcome,
        )
    except PaymentError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    return payment


@router.post("/webhook/", response_model=WebhookAck)
async def payment_webhook(
    payload: WebhookPayload,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Simulated payment-provider webhook. No auth header is required here
    (a real provider would sign the payload with a shared secret instead --
    see README "what I'd improve"), but the endpoint is fully idempotent:
    replaying the same event_id any number of times is guaranteed to be a
    no-op after the first successful application.
    """
    raw_body = (await request.body()).decode("utf-8", errors="replace")

    try:
        payment, duplicate = process_webhook_event(
            db,
            event_id=payload.event_id,
            provider_reference=payload.provider_reference,
            new_status=payload.status,
            raw_payload=raw_body,
        )
    except PaymentError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    booking_status = payment.booking.status if payment else None
    return WebhookAck(received=True, duplicate=duplicate, booking_status=booking_status)
