import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import stripe

from backend import patient_portal, payments, stripe_webhook
from backend.settings import Settings
from test_api import api, create, onboard, token

SECRET = "whsec_test"


def signed_header(payload: bytes, secret: str, timestamp: int) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


class FakeStripe:
    """In-memory Checkout Sessions and Refunds with Stripe's state rules."""

    def __init__(self):
        self.sessions = {}
        self.refunds = []

    def create(self, **params):
        session_id = f"cs_test_{uuid.uuid4().hex[:10]}"
        self.sessions[session_id] = {"status": "open", "amount": params["line_items"][0]["price_data"]["unit_amount"],
                                     "params": params}
        return SimpleNamespace(id=session_id, url=f"https://checkout.stripe.test/{session_id}")

    def retrieve(self, session_id):
        return SimpleNamespace(id=session_id, status=self.sessions[session_id]["status"])

    def expire(self, session_id):
        if self.sessions[session_id]["status"] != "open":
            raise stripe.error.InvalidRequestError("Session is not open", None)
        self.sessions[session_id]["status"] = "expired"

    def refund(self, **params):
        self.refunds.append(params)
        return SimpleNamespace(id=f"re_{uuid.uuid4().hex[:10]}")


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeStripe()
    settings = Settings(_env_file=None, stripe_secret_key="sk_test_fake", stripe_webhook_secret=SECRET)
    for module in (patient_portal, payments, stripe_webhook):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(stripe.checkout.Session, "create", fake.create)
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", fake.retrieve)
    monkeypatch.setattr(stripe.checkout.Session, "expire", fake.expire)
    monkeypatch.setattr(stripe.Refund, "create", fake.refund)
    return fake


async def booked_appointment(client, amount=2500):
    patient_user, admin, hospital = await onboard(client)
    department = await create(client, "departments", admin, {"name": "Medicine"})
    doctor = await create(client, "doctors", admin, {"name": "Dr Stripe", "department_id": department["id"]})
    room = await create(client, "rooms", admin, {"name": "Consultation"})
    starts = datetime.now(timezone.utc) + timedelta(days=1)
    schedule = await create(client, "schedules", admin, {
        "doctor_id": doctor["id"], "room_id": room["id"],
        "starts_at": starts.isoformat(), "ends_at": (starts + timedelta(hours=1)).isoformat(),
    })
    patient = {"Authorization": f"Bearer {token(patient_user)}"}
    assert (await client.post("/v1/patient/profiles", headers=patient, json={
        "branch_id": hospital["branch"]["id"], "full_name": "Stripe Patient", "mobile": "0771234567",
    })).status_code == 201
    appointment_id = (await client.post("/v1/patient/appointments", headers=patient, json={
        "schedule_id": schedule["id"]})).json()["id"]
    response = await client.patch(f"/v1/appointments/{appointment_id}", headers=admin, json={
        "status": "BOOKED", "quotation": [{"name": "Consultation fee", "amount": amount}]})
    assert response.status_code == 200, response.text
    return appointment_id, admin, patient


async def checkout(client, appointment_id, patient):
    response = await client.post(f"/v1/patient/appointments/{appointment_id}/payment", headers=patient,
                                 json={"method": "ONLINE"})
    assert response.status_code == 200, response.text
    return response.json()["checkout_url"].rsplit("/", 1)[1]


async def deliver(client, appointment_id, session_id, amount, payment_intent="pi_test_123", currency="lkr"):
    event = {"id": f"evt_{uuid.uuid4().hex}", "type": "checkout.session.completed", "data": {"object": {
        "id": session_id, "client_reference_id": appointment_id, "payment_status": "paid",
        "payment_intent": payment_intent, "amount_total": amount, "currency": currency}}}
    payload = json.dumps(event).encode()
    response = await client.post("/v1/webhooks/stripe", content=payload,
                                 headers={"stripe-signature": signed_header(payload, SECRET, int(time.time()))})
    assert response.status_code == 200, response.text
    return payload


async def payment(client, appointment_id, patient):
    overview = (await client.get("/v1/patient/overview", headers=patient)).json()
    return next(item for item in overview["appointments"] if item["id"] == appointment_id)


async def test_matching_payment_settles_once_and_locks_the_quotation(api, fake_stripe):
    client, _ = api
    appointment_id, admin, patient = await booked_appointment(client)
    session_id = await checkout(client, appointment_id, patient)
    assert fake_stripe.sessions[session_id]["amount"] == 250000
    assert fake_stripe.sessions[session_id]["params"]["client_reference_id"] == appointment_id

    payload = await deliver(client, appointment_id, session_id, 250000)
    replay = await client.post("/v1/webhooks/stripe", content=payload,
                               headers={"stripe-signature": signed_header(payload, SECRET, int(time.time()))})
    assert replay.status_code == 200
    paid = await payment(client, appointment_id, patient)
    assert (paid["payment_status"], paid["payment_reference"]) == ("PAID", "pi_test_123")

    changed = await client.patch(f"/v1/appointments/{appointment_id}", headers=admin, json={
        "status": "BOOKED", "quotation": [{"name": "Consultation fee", "amount": 5000}]})
    assert changed.status_code == 409


async def test_stale_checkout_after_quotation_change_is_never_marked_paid(api, fake_stripe):
    client, _ = api
    appointment_id, admin, patient = await booked_appointment(client, amount=1000)
    old_session = await checkout(client, appointment_id, patient)

    changed = await client.patch(f"/v1/appointments/{appointment_id}", headers=admin, json={
        "status": "BOOKED", "quotation": [{"name": "Surgery package", "amount": 5000}]})
    assert changed.status_code == 200, changed.text
    assert fake_stripe.sessions[old_session]["status"] == "expired"
    assert changed.json()["payment_status"] == "UNPAID"

    # Even if Stripe still reports the old Rs 1,000 checkout as paid, it cannot settle Rs 5,000.
    await deliver(client, appointment_id, old_session, 100000)
    flagged = await payment(client, appointment_id, patient)
    assert flagged["payment_status"] == "REVIEW_REQUIRED"

    refunded = await client.post(f"/v1/appointments/{appointment_id}/payment-resolution", headers=admin,
                                 json={"action": "REFUND", "note": "Paid the superseded quotation"})
    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["payment_status"] == "REFUNDED"
    assert fake_stripe.refunds[0]["payment_intent"] == "pi_test_123"


async def test_amount_or_currency_mismatch_requires_review(api, fake_stripe):
    client, _ = api
    appointment_id, admin, patient = await booked_appointment(client)
    session_id = await checkout(client, appointment_id, patient)
    await deliver(client, appointment_id, session_id, 100)
    assert (await payment(client, appointment_id, patient))["payment_status"] == "REVIEW_REQUIRED"
    # Staff can confirm a reviewed payment explicitly; the patient cannot re-pay meanwhile.
    assert (await client.post(f"/v1/patient/appointments/{appointment_id}/payment", headers=patient,
                              json={"method": "ONLINE"})).status_code == 409
    accepted = await client.post(f"/v1/appointments/{appointment_id}/payment-resolution", headers=admin,
                                 json={"action": "ACCEPT"})
    assert accepted.status_code == 200 and accepted.json()["payment_status"] == "PAID"

    other_id, _, other_patient = await booked_appointment(client)
    other_session = await checkout(client, other_id, other_patient)
    await deliver(client, other_id, other_session, 250000, payment_intent="pi_other", currency="usd")
    assert (await payment(client, other_id, other_patient))["payment_status"] == "REVIEW_REQUIRED"


async def test_new_checkout_expires_the_previous_one(api, fake_stripe):
    client, _ = api
    appointment_id, _, patient = await booked_appointment(client)
    first = await checkout(client, appointment_id, patient)
    second = await checkout(client, appointment_id, patient)
    assert fake_stripe.sessions[first]["status"] == "expired"
    await deliver(client, appointment_id, first, 250000)
    assert (await payment(client, appointment_id, patient))["payment_status"] == "REVIEW_REQUIRED"
    assert second != first


async def test_quotation_cannot_change_while_checkout_is_completing(api, fake_stripe):
    client, _ = api
    appointment_id, admin, patient = await booked_appointment(client)
    session_id = await checkout(client, appointment_id, patient)
    fake_stripe.sessions[session_id]["status"] = "complete"  # paid, webhook not yet received
    changed = await client.patch(f"/v1/appointments/{appointment_id}", headers=admin, json={
        "status": "BOOKED", "quotation": [{"name": "Consultation fee", "amount": 9000}]})
    assert changed.status_code == 409


async def test_cancelling_a_paid_appointment_flags_and_completes_a_refund(api, fake_stripe):
    client, admin_client = api
    appointment_id, admin, patient = await booked_appointment(client)
    session_id = await checkout(client, appointment_id, patient)
    await deliver(client, appointment_id, session_id, 250000)
    cancelled = await client.patch(f"/v1/patient/appointments/{appointment_id}/cancel", headers=patient)
    assert cancelled.status_code == 200, cancelled.text
    assert (await payment(client, appointment_id, patient))["payment_status"] == "REFUND_REQUIRED"
    assert (await client.post(f"/v1/appointments/{appointment_id}/payment-resolution", headers=admin,
                              json={"action": "ACCEPT"})).status_code == 409
    refunded = await client.post(f"/v1/appointments/{appointment_id}/payment-resolution", headers=admin,
                                 json={"action": "REFUND"})
    assert refunded.status_code == 200 and refunded.json()["payment_status"] == "REFUNDED"
    assert fake_stripe.refunds[0]["idempotency_key"] == f"appointment-refund-{appointment_id}"


async def test_payment_after_cancellation_requires_refund(api, fake_stripe):
    client, _ = api
    appointment_id, admin, patient = await booked_appointment(client)
    session_id = await checkout(client, appointment_id, patient)
    fake_stripe.sessions[session_id]["status"] = "complete"
    assert (await client.patch(f"/v1/patient/appointments/{appointment_id}/cancel", headers=patient)).status_code == 200
    await deliver(client, appointment_id, session_id, 250000)
    assert (await payment(client, appointment_id, patient))["payment_status"] == "REFUND_REQUIRED"


async def test_stripe_webhook_rejects_invalid_signature(api, monkeypatch):
    client, _ = api
    settings = Settings(_env_file=None, stripe_webhook_secret=SECRET)
    monkeypatch.setattr(stripe_webhook, "get_settings", lambda: settings)
    response = await client.post("/v1/webhooks/stripe", content=b"{}", headers={
        "stripe-signature": "t=1700000000,v1=invalid",
    })
    assert response.status_code == 400
