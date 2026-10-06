"""Appointment payment rules shared by the patient portal, staff inbox and Stripe webhook."""
import asyncio
from decimal import ROUND_HALF_UP, Decimal

import stripe
from fastapi import HTTPException

from .settings import get_settings

CURRENCY = "lkr"
# Money has been received (or is under review); the quotation can no longer change.
PAYMENT_LOCKED = {"PAID", "REVIEW_REQUIRED", "REFUND_REQUIRED", "REFUNDED"}
# Received money that the hospital still has to settle with the patient.
REFUNDABLE = {"PAID", "REVIEW_REQUIRED"}


def quotation_total(quotation: list | None) -> Decimal:
    return sum((Decimal(str(item.get("amount", 0))) for item in (quotation or [])), Decimal(0))


def quotation_minor_units(quotation: list | None) -> int:
    """LKR cents, the unit Stripe charges and reports in."""
    return int((quotation_total(quotation) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def configure_stripe() -> None:
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Online payments are not configured by this hospital")
    stripe.api_key = settings.stripe_secret_key
    # Timeouts are an HTTP client setting; passing `timeout=` to a Stripe call would be
    # sent to the API as an unknown parameter and rejected.
    if not getattr(stripe.default_http_client, "_mediqueue_timeout", False):
        client = stripe.new_default_http_client(timeout=10)
        client._mediqueue_timeout = True
        stripe.default_http_client = client


async def close_checkout(session_id: str) -> bool:
    """Make a Checkout Session unpayable. False means it already completed (money taken)."""
    configure_stripe()
    try:
        session = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
        if session.status == "open":
            try:
                await asyncio.to_thread(stripe.checkout.Session.expire, session_id)
                return True
            except stripe.error.InvalidRequestError:
                # Completed between the read and the expiry: re-read the final state.
                session = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
        return session.status == "expired"
    except stripe.error.StripeError as exc:
        raise HTTPException(502, "Stripe could not update the payment session") from exc


def clear_checkout(appointment) -> None:
    appointment.payment_status = "UNPAID"
    appointment.payment_method = None
    appointment.payment_reference = None
    appointment.checkout_session_id = None
    appointment.payment_amount = None


async def invalidate_checkout(appointment) -> None:
    """Cancel an in-flight online checkout before its amount or appointment changes."""
    if appointment.payment_status != "CHECKOUT_STARTED":
        return
    if appointment.checkout_session_id and not await close_checkout(appointment.checkout_session_id):
        raise HTTPException(409, "A payment for this appointment is being completed. Refresh in a moment.")
    clear_checkout(appointment)


async def refund_online_payment(appointment) -> str:
    """Refund the full captured Stripe payment; idempotent per appointment."""
    configure_stripe()
    payment_intent = appointment.payment_reference
    if not payment_intent or not payment_intent.startswith("pi_"):
        raise HTTPException(409, "No Stripe payment is recorded for this appointment")
    try:
        refund = await asyncio.to_thread(stripe.Refund.create, payment_intent=payment_intent,
                                         idempotency_key=f"appointment-refund-{appointment.id}")
    except stripe.error.StripeError as exc:
        raise HTTPException(502, "Stripe could not create the refund") from exc
    return str(refund.id)
