"""Hospital discovery, capacity-aware self booking and sanitized queue estimates."""
from datetime import datetime, timedelta, timezone
import uuid
from backend.models import AuditEvent, QueueToken
from test_api import api, onboard, create, token


def test_existing_local_sqlite_locations_upgrade_without_data_loss():
    from sqlalchemy import create_engine, text
    from backend.db import ensure_sqlite_discovery_columns
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS iam"))
        connection.execute(text("ATTACH DATABASE ':memory:' AS queue"))
        connection.execute(text("CREATE TABLE iam.branch (id TEXT PRIMARY KEY, name TEXT)"))
        connection.execute(text("INSERT INTO iam.branch VALUES ('existing', 'Existing hospital')"))
        connection.execute(text('CREATE TABLE queue."queue" (id TEXT PRIMARY KEY, name TEXT)'))
        connection.execute(text('INSERT INTO queue."queue" VALUES (\'queue\', \'Registration\')'))
        ensure_sqlite_discovery_columns(connection)
        ensure_sqlite_discovery_columns(connection)
        branch = connection.execute(text("SELECT name, address, latitude, longitude FROM iam.branch")).one()
        assert tuple(branch) == ("Existing hospital", "", None, None)
        assert connection.execute(text('SELECT average_service_minutes FROM queue."queue"')).scalar_one() == 5
    engine.dispose()


async def test_hospital_locations_are_validated_and_admin_scoped(api):
    client, _ = api
    _, admin, hospital = await onboard(client, "Central Hospital")
    _, other, _ = await onboard(client, "Other Hospital")
    branch_id = hospital["branch"]["id"]
    location = {"name": "Main", "address": "10 Hospital Road", "phone": "+94112345678", "latitude": 6.9271, "longitude": 79.8612}
    assert (await client.put(f"/v1/branches/{branch_id}", headers=other, json=location)).status_code == 403
    assert (await client.put(f"/v1/branches/{branch_id}", headers=admin, json={**location, "longitude": 200})).status_code == 422
    assert (await client.put(f"/v1/branches/{branch_id}", headers=admin, json={"name": "Main", "latitude": 6})).status_code == 422
    saved = await client.put(f"/v1/branches/{branch_id}", headers=admin, json=location)
    assert saved.status_code == 200, saved.text
    patient = {"Authorization": f"Bearer {token(uuid.uuid4())}"}
    centers = (await client.get("/v1/patient/centers", headers=patient)).json()
    assert len(centers) == 2
    center = next(c for c in centers if c["id"] == branch_id)
    assert center["latitude"] == location["latitude"] and center["longitude"] == location["longitude"]
    assert center["address"] == location["address"] and center["hospital"] == "Central Hospital"


async def test_patient_sessions_show_remaining_capacity_and_own_booking(api):
    client, _ = api
    _, admin, hospital = await onboard(client)
    branch_id = hospital["branch"]["id"]
    department = await create(client, "departments", admin, {"name": "Medicine"})
    doctor = await create(client, "doctors", admin, {"name": "Doctor Perera", "department_id": department["id"], "specialty": "Medicine"})
    room = await create(client, "rooms", admin, {"name": "Room 1"})
    now = datetime.now(timezone.utc)
    schedule = await create(client, "schedules", admin, {"doctor_id": doctor["id"], "room_id": room["id"], "starts_at": (now + timedelta(hours=1)).isoformat(), "ends_at": (now + timedelta(hours=2)).isoformat(), "capacity": 1})
    owner = {"Authorization": f"Bearer {token(uuid.uuid4())}"}
    other = {"Authorization": f"Bearer {token(uuid.uuid4())}"}
    path = f"/v1/patient/schedules/{branch_id}"
    before = (await client.get(path, headers=owner)).json()[0]
    assert before["remaining"] == 1 and before["already_booked"] is False
    assert (await client.post("/v1/patient/appointments", headers=owner, json={"schedule_id": schedule["id"]})).status_code == 403
    for identity in (owner, other):
        assert (await client.post("/v1/patient/profiles", headers=identity, json={"branch_id": branch_id, "full_name": "Patient", "mobile": "0771234567"})).status_code == 201
    booked = await client.post("/v1/patient/appointments", headers=owner, json={"schedule_id": schedule["id"]})
    assert booked.status_code == 201, booked.text
    after = (await client.get(path, headers=owner)).json()[0]
    assert after["remaining"] == 0 and after["already_booked"] is True
    assert (await client.get(path, headers=other)).json()[0]["already_booked"] is False
    assert (await client.post("/v1/patient/appointments", headers=other, json={"schedule_id": schedule["id"]})).status_code == 409
    assert (await client.get("/v1/patient/overview", headers=other)).json()["appointments"] == []
    overview = (await client.get("/v1/patient/overview", headers=owner)).json()
    assert overview["appointments"][0]["center"] == "Test Hospital · Main branch"
    await client.patch(f'/v1/patient/appointments/{booked.json()["id"]}/cancel', headers=owner)
    assert (await client.get(path, headers=other)).json()[0]["remaining"] == 1


async def test_live_queue_counts_estimates_and_patient_privacy(api):
    client, sessions = api
    _, admin, hospital = await onboard(client)
    branch_id = hospital["branch"]["id"]
    queue = await create(client, "queues", admin, {"name": "Registration", "service_type": "REGISTRATION", "average_service_minutes": 7})
    owner = {"Authorization": f"Bearer {token(uuid.uuid4())}", "Idempotency-Key": "owner"}
    await client.post("/v1/patient/profiles", headers=owner, json={"branch_id": branch_id, "full_name": "Private patient name", "mobile": "0771234567"})
    first = await client.post(f'/v1/queues/{queue["id"]}/tokens', headers={**admin, "Idempotency-Key": "first"}, json={"patient_ref": "private-first"})
    assert first.status_code == 201, first.text
    await client.post(f'/v1/patient/queues/{queue["id"]}/tickets', headers=owner)
    path = f"/v1/patient/queue-status/{branch_id}"
    summary_response = await client.get(path, headers=owner)
    summary = summary_response.json()[0]
    assert summary["waiting_count"] == 2 and summary["estimated_wait_minutes"] == 14
    assert summary["estimate_source"] == "configured_average"
    assert "private" not in summary_response.text.lower()
    own = (await client.get("/v1/patient/tickets", headers=owner)).json()[0]
    assert own["ahead"] == 1 and own["estimated_wait_minutes"] == 7
    await client.post(f'/v1/queues/{queue["id"]}/call-next', headers={**admin, "Idempotency-Key": "call"})
    summary = (await client.get(path, headers=owner)).json()[0]
    assert summary["waiting_count"] == 1 and summary["serving_count"] == 1
    own = (await client.get("/v1/patient/tickets", headers=owner)).json()[0]
    assert own["ahead"] == 0 and own["estimated_wait_minutes"] == 7
    # Three measured services select the median; waiting time is not included.
    for index, minutes in enumerate((3, 4, 5)):
        completed = await client.post(f'/v1/queues/{queue["id"]}/tokens', headers={**admin, "Idempotency-Key": f"sample-{index}"}, json={"patient_ref": f"sample-{index}"})
        async with sessions() as db:
            row = await db.get(QueueToken, uuid.UUID(completed.json()["id"]))
            row.status = "COMPLETED"
            end = datetime.now(timezone.utc) - timedelta(minutes=10)
            db.add_all([AuditEvent(tenant_id=row.tenant_id, actor_id="staff", action="start", entity_id=row.id, payload={}, created_at=end - timedelta(minutes=minutes)), AuditEvent(tenant_id=row.tenant_id, actor_id="staff", action="complete", entity_id=row.id, payload={}, created_at=end)])
            await db.commit()
    summary = (await client.get(path, headers=owner)).json()[0]
    assert summary["estimate_source"] == "recent_service_times"
    assert summary["average_service_minutes"] == 4 and summary["estimated_wait_minutes"] == 8
