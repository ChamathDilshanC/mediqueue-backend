"""Ownership, branch isolation, review lifecycle and reserved capacity."""
import uuid
from datetime import datetime, timedelta, timezone
from test_api import api, onboard, create, token


async def test_patient_requests_staff_review_capacity_and_attendance(api):
    client, _ = api
    _, admin, hospital = await onboard(client)
    _, other_admin, _ = await onboard(client, "Other hospital")
    branch = hospital["branch"]["id"]
    department = await create(client, "departments", admin, {"name": "Medicine"})
    doctor = await create(client, "doctors", admin, {"name": "Dr Chamath", "department_id": department["id"]})
    room = await create(client, "rooms", admin, {"name": "Consultation 1"})
    starts = datetime.now(timezone.utc) + timedelta(days=1)
    schedule = await create(client, "schedules", admin, {"doctor_id": doctor["id"], "room_id": room["id"],
        "starts_at": starts.isoformat(), "ends_at": (starts + timedelta(hours=2)).isoformat(), "capacity": 1})
    owner = {"Authorization": f"Bearer {token(uuid.uuid4())}"}
    second = {"Authorization": f"Bearer {token(uuid.uuid4())}"}
    for headers, name in ((owner, "Patient Chamath"), (second, "Patient Two")):
        result = await client.post("/v1/patient/profiles", headers=headers, json={"branch_id": branch, "full_name": name, "mobile": "0771234567"})
        assert result.status_code == 201, result.text
    body = {"schedule_id": schedule["id"]}
    requested = await client.post("/v1/patient/appointments", headers=owner, json=body)
    assert requested.status_code == 201 and requested.json()["status"] == "PENDING"
    path = f'/v1/appointments/{requested.json()["id"]}'
    assert (await client.post("/v1/patient/appointments", headers=second, json=body)).status_code == 409
    assert (await client.patch(path, headers=owner, json={"status": "BOOKED"})).status_code == 403
    assert (await client.patch(path, headers=other_admin, json={"status": "BOOKED"})).status_code == 404
    inbox = await client.get("/v1/appointment-inbox?status=PENDING&q=Chamath", headers=admin)
    assert inbox.status_code == 200, inbox.text
    assert inbox.json()["total"] == 1
    row = inbox.json()["items"][0]
    assert row["source"] == "PATIENT" and row["doctor"] == "Dr Chamath" and row["room"] == "Consultation 1"
    assert (await client.get("/v1/appointment-inbox", headers=other_admin)).json()["total"] == 0
    assert (await client.patch(path, headers=admin, json={"status": "CHECKED_IN"})).status_code == 409
    assert (await client.patch(path, headers=admin, json={"status": "REJECTED"})).status_code == 422
    rejected = await client.patch(path, headers=admin, json={"status": "REJECTED", "reason": "Please select another session"})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["reviewed_by"] and rejected.json()["reviewed_at"]
    overview = (await client.get("/v1/patient/overview", headers=owner)).json()
    assert overview["appointments"][0]["review_reason"] == "Please select another session"
    assert (await client.patch(path, headers=admin, json={"status": "BOOKED"})).status_code == 409
    # Rejection releases capacity and patients can request again.
    request2 = await client.post("/v1/patient/appointments", headers=owner, json=body)
    assert request2.status_code == 201, request2.text
    path2 = f'/v1/appointments/{request2.json()["id"]}'
    for state in ("BOOKED", "CHECKED_IN", "COMPLETED"):
        result = await client.patch(path2, headers=admin, json={"status": state})
        assert result.status_code == 200 and result.json()["status"] == state, result.text
    assert (await client.patch(path2, headers=admin, json={"status": "CANCELLED"})).status_code == 409
    assert (await client.get("/v1/patient/overview", headers=owner)).json()["appointments"][0]["status"] in {"COMPLETED", "REJECTED"}
    history = (await client.get("/v1/appointment-inbox?limit=1&offset=1", headers=admin)).json()
    assert history["total"] == 2 and len(history["items"]) == 1
    events = (await client.get("/v1/audit-events?limit=200", headers=admin)).json()
    assert {"appointment.requested", "appointment.rejected", "appointment.booked", "appointment.checked_in", "appointment.completed"} <= {e["action"] for e in events}
    next_schedule = await create(client, "schedules", admin, {"doctor_id": doctor["id"], "room_id": room["id"],
        "starts_at": (starts + timedelta(days=1)).isoformat(), "ends_at": (starts + timedelta(days=1, hours=2)).isoformat(), "capacity": 1})
    next_body = {"schedule_id": next_schedule["id"]}
    pending = await client.post("/v1/patient/appointments", headers=owner, json=next_body)
    next_id = pending.json()["id"]
    # Cancellation before staff approval cannot be overwritten by a stale decision.
    assert (await client.patch(f"/v1/patient/appointments/{next_id}/cancel", headers=owner)).status_code == 200
    assert (await client.patch(f"/v1/appointments/{next_id}", headers=admin, json={"status": "BOOKED"})).status_code == 409
    fresh = await client.post("/v1/patient/appointments", headers=owner, json=next_body)
    assert fresh.status_code == 201, fresh.text
    fresh_path = f'/v1/appointments/{fresh.json()["id"]}'
    for state in ("BOOKED", "NO_SHOW"):
        assert (await client.patch(fresh_path, headers=admin, json={"status": state})).status_code == 200
    assert any(a["status"] == "NO_SHOW" for a in (await client.get("/v1/patient/overview", headers=owner)).json()["appointments"])
    doctor_user = uuid.uuid4()
    doctor_headers = {"Authorization": f"Bearer {token(doctor_user)}"}
    await client.get("/v1/auth/me", headers=doctor_headers)
    await create(client, "memberships", admin, {"user_id": str(doctor_user), "role": "doctor"})
    doctor_headers.update({key: value for key, value in admin.items() if key.startswith("X-")})
    assert (await client.get("/v1/appointment-inbox", headers=doctor_headers)).status_code == 200
    assert (await client.patch(fresh_path, headers=doctor_headers, json={"status": "CANCELLED"})).status_code == 403
