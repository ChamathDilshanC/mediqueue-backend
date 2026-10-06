"""Stripe webhook verification and idempotent, amount-checked payment settlement."""
import uuid
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from .db import get_session
from .models import Appointment, AuditEvent, StripeWebhookEvent
from .payments import CURRENCY, PAYMENT_LOCKED, quotation_minor_units
from .settings import get_settings

router = APIRouter(tags=["Stripe"])
SETTLED = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
CLOSED_APPOINTMENTS = {"CANCELLED", "REJECTED"}


def field(value, name, default=None):
    return value[name] if name in value else default


def record(db, appointment, action, session, **details):
    db.add(AuditEvent(tenant_id=appointment.tenant_id, actor_id="stripe", action=action, entity_id=appointment.id,
                      payload={"branch_id": str(appointment.branch_id), "checkout_session_id": str(field(session, "id") or ""),
                               "amount_total": field(session, "amount_total"), "currency": field(session, "currency"),
                               **details}))


def settle(db, appointment, session):
    """Mark PAID only when this is the appointment's current session, amount and currency."""
    received = field(session, "amount_total")
    expected = quotation_minor_units(appointment.quotation)
    payment_intent = str(field(session, "payment_intent") or field(session, "id") or "")
    if appointment.payment_status in PAYMENT_LOCKED:
        if appointment.payment_reference != payment_intent:
            # Another captured payment while one is already recorded: keep the recorded one
            # untouched and leave an auditable trail so staff refund this one in Stripe.
            record(db, appointment, "payment.duplicate_received", session, payment_intent=payment_intent)
        return  # otherwise the same payment reported again (completed + async_payment_succeeded)
    matches = (field(session, "id") == appointment.checkout_session_id
               and isinstance(received, int) and received == appointment.payment_amount == expected
               and str(field(session, "currency") or "").lower() == CURRENCY)
    appointment.payment_method = "ONLINE"
    appointment.payment_reference = payment_intent
    if appointment.status in CLOSED_APPOINTMENTS:
        appointment.payment_status = "REFUND_REQUIRED"
        record(db, appointment, "payment.received_for_closed_appointment", session, expected_amount=expected)
    elif matches:
        appointment.payment_status = "PAID"
        record(db, appointment, "payment.paid", session)
    else:
        # Money was captured but does not match the current quotation/session: staff decide.
        appointment.payment_status = "REVIEW_REQUIRED"
        record(db, appointment, "payment.mismatch", session, expected_amount=expected,
               expected_session_id=appointment.checkout_session_id)


@router.post("/v1/webhooks/stripe")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_session)):
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise HTTPException(503, "Stripe webhooks are not configured")
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    if not signature:
        raise HTTPException(400, "Missing Stripe signature")
    try:
        event = stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)
    except ValueError as exc:
        raise HTTPException(400, "Invalid Stripe payload") from exc
    except stripe.error.SignatureVerificationError as exc:
        raise HTTPException(400, "Invalid Stripe signature") from exc

    event_id = str(event["id"])
    event_type = str(event["type"])
    if await db.scalar(select(StripeWebhookEvent).where(StripeWebhookEvent.event_id == event_id)):
        return {"received": True}

    if event_type in SETTLED or event_type == "checkout.session.expired":
        session = event["data"]["object"]
        appointment_id = field(session, "client_reference_id")
        if not appointment_id:
            raise HTTPException(400, "Stripe session is missing appointment reference")
        try:
            appointment_uuid = uuid.UUID(str(appointment_id))
        except ValueError as exc:
            raise HTTPException(400, "Stripe session has an invalid appointment reference") from exc
        appointment = await db.scalar(select(Appointment).where(Appointment.id == appointment_uuid).with_for_update())
        if not appointment:
            raise HTTPException(404, "Appointment for Stripe session was not found")
        if event_type == "checkout.session.expired":
            if appointment.payment_status == "CHECKOUT_STARTED" and appointment.checkout_session_id == field(session, "id"):
                appointment.payment_status = "UNPAID"
                appointment.payment_method = None
                appointment.checkout_session_id = None
                appointment.payment_amount = None
        elif field(session, "payment_status") == "paid":
            settle(db, appointment, session)

    db.add(StripeWebhookEvent(event_id=event_id, event_type=event_type))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return {"received": True}
    return {"received": True}
