import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from backend import stripe_webhook
from backend.settings import Settings
from test_api import api, create, onboard, token


def signed_header(payload: bytes, secret: str, timestamp: int) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


async def test_stripe_webhook_verifies_and_is_idempotent(api, monkeypatch):
    client, _ = api
    patient_user, admin, hospital = await onboard(client)
    department = await create(client, "departments", admin, {"name": "Medicine"})
    doctor = await create(client, "doctors", admin, {"name": "Dr Stripe", "department_id": department["id"]})
    room = await create(client, "rooms", admin, {"name": "Consultation"})
    starts = datetime.now(timezone.utc) + timedelta(days=1)
    schedule = await create(client, "schedules", admin, {
        "doctor_id": doctor["id"], "room_id": room["id"],
        "starts_at": starts.isoformat(), "ends_at": (starts + timedelta(hours=1)).isoformat(),
    })
    patient_headers = {"Authorization": f"Bearer {token(patient_user)}"}
    assert (await client.post("/v1/patient/profiles", headers=patient_headers, json={
        "branch_id": hospital["branch"]["id"], "full_name": "Stripe Patient", "mobile": "0771234567",
    })).status_code == 201
    appointment = await client.post("/v1/patient/appointments", headers=patient_headers, json={
        "schedule_id": schedule["id"],
    })
    appointment_id = appointment.json()["id"]
    assert (await client.patch(f"/v1/appointments/{appointment_id}", headers=admin, json={
        "status": "BOOKED",
    })).status_code == 200
    assert (await client.patch(f"/v1/appointments/{appointment_id}", headers=admin, json={
        "status": "BOOKED", "quotation": [{"name": "Consultation fee", "amount": 2500}],
    })).status_code == 200

    secret = "whsec_test"
    settings = Settings(_env_file=None, stripe_webhook_secret=secret)
    monkeypatch.setattr(stripe_webhook, "get_settings", lambda: settings)
    event = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_123",
            "client_reference_id": appointment_id,
            "payment_status": "paid",
            "payment_intent": "pi_test_123",
        }},
    }
    payload = json.dumps(event).encode()
    headers = {"stripe-signature": signed_header(payload, secret, int(time.time()))}
    first = await client.post("/v1/webhooks/stripe", content=payload, headers=headers)
    assert first.status_code == 200, first.text
    duplicate = await client.post("/v1/webhooks/stripe", content=payload, headers=headers)
    assert duplicate.status_code == 200, duplicate.text
    overview = (await client.get("/v1/patient/overview", headers=patient_headers)).json()
    paid = next(item for item in overview["appointments"] if item["id"] == appointment_id)
    assert paid["payment_status"] == "PAID"
    assert paid["payment_reference"] == "pi_test_123"


async def test_stripe_webhook_rejects_invalid_signature(api, monkeypatch):
    client, _ = api
    secret = "whsec_test"
    settings = Settings(_env_file=None, stripe_webhook_secret=secret)
    monkeypatch.setattr(stripe_webhook, "get_settings", lambda: settings)
    response = await client.post("/v1/webhooks/stripe", content=b"{}", headers={
        "stripe-signature": "t=1700000000,v1=invalid",
    })
    assert response.status_code == 400
