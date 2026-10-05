"""Stripe webhook verification and idempotent payment settlement."""
import uuid
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from .db import get_session
from .models import Appointment, StripeWebhookEvent
from .settings import get_settings

router = APIRouter(tags=["Stripe"])

def field(value, name, default=None):
    return value[name] if name in value else default


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

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        appointment_id = field(session, "client_reference_id")
        if not appointment_id:
            raise HTTPException(400, "Stripe session is missing appointment reference")
        try:
            appointment_uuid = uuid.UUID(str(appointment_id))
        except ValueError as exc:
            raise HTTPException(400, "Stripe session has an invalid appointment reference") from exc
        appointment = await db.get(Appointment, appointment_uuid, with_for_update=True)
        if not appointment:
            raise HTTPException(404, "Appointment for Stripe session was not found")
        if field(session, "payment_status") == "paid":
            appointment.payment_status = "PAID"
            appointment.payment_method = "ONLINE"
            appointment.payment_reference = str(field(session, "payment_intent") or field(session, "id") or "")

    db.add(StripeWebhookEvent(event_id=event_id, event_type=event_type))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return {"received": True}
    return {"received": True}
