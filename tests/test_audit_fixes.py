"""Regression tests for the security/logic audit fixes."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend import auth
from backend.models import OutboxEvent
from test_api import SYSTEM_ADMIN_EMAIL, api, create, onboard, system_admin_headers, token
from worker import outbox


async def test_hospitals_are_provisioned_only_by_verified_system_admins(api):
    client, _ = api
    user = uuid.uuid4()
    headers = {"Authorization": f"Bearer {token(user, email='someone@example.test')}"}
    assert (await client.get("/v1/auth/me", headers=headers)).status_code == 200
    # Calling the API directly must not bypass the hospital application review.
    assert (await client.post("/v1/hospitals", headers=headers, json={"name": "Self made"})).status_code == 403
    assert (await client.get("/v1/admin/hospital-applications", headers=headers)).status_code == 403
    unconfirmed = system_admin_headers(test_confirmed_at=None)
    assert (await client.get("/v1/admin/hospital-applications", headers=unconfirmed)).status_code == 403
    other_case = {"Authorization": f"Bearer {token(uuid.uuid4(), email=SYSTEM_ADMIN_EMAIL.title())}"}
    assert (await client.get("/v1/admin/hospital-applications", headers=other_case)).status_code == 200
    missing = await client.post("/v1/hospitals", headers=system_admin_headers(),
                                json={"name": "Hospital", "admin_user_id": str(uuid.uuid4())})
    assert missing.status_code == 404
    created = await client.post("/v1/hospitals", headers=system_admin_headers(),
                                json={"name": "Hospital", "admin_user_id": str(user)})
    assert created.status_code == 201
    assert created.json()["membership"]["user_id"] == str(user)


async def test_ward_with_beds_or_admissions_cannot_be_deleted(api):
    client, _ = api
    _, admin, _ = await onboard(client)
    department = await create(client, "departments", admin, {"name": "Surgery"})
    ward = await create(client, "wards", admin, {"name": "Ward A", "department_id": department["id"], "bed_capacity": 2})
    response = await client.delete(f"/v1/wards/{ward['id']}", headers=admin)
    assert response.status_code == 409
    assert len((await client.get("/v1/beds", headers=admin)).json()) == 2


async def test_only_completed_ward_tasks_can_be_verified(api):
    client, _ = api
    _, admin, _ = await onboard(client)
    department = await create(client, "departments", admin, {"name": "Medicine"})
    ward = await create(client, "wards", admin, {"name": "Ward B", "department_id": department["id"], "bed_capacity": 1})
    nurse = await create(client, "nurses", admin, {"employee_id": "N-1", "name": "Nurse One"})
    body = {"ward_id": ward["id"], "nurse_id": nurse["id"], "task_type": "WARD_WALK",
            "scheduled_at": datetime.now(timezone.utc).isoformat()}
    assert (await client.post("/v1/ward-tasks", headers=admin, json={**body, "status": "VERIFIED"})).status_code == 422
    task = await create(client, "ward-tasks", admin, {**body, "verified_by": "forged"})
    assert task["verified_by"] == ""
    path = f"/v1/ward-tasks/{task['id']}"
    assert (await client.post(f"{path}/verify", headers=admin)).status_code == 409
    assert (await client.put(path, headers=admin, json={**body, "status": "IN_PROGRESS"})).status_code == 200
    assert (await client.post(f"{path}/verify", headers=admin)).status_code == 409
    assert (await client.put(path, headers=admin, json={**body, "status": "VERIFIED"})).status_code == 422
    completed = await client.put(path, headers=admin, json={**body, "status": "COMPLETED"})
    assert completed.status_code == 200 and completed.json()["completed_at"]
    verified = await client.post(f"{path}/verify", headers=admin)
    assert verified.status_code == 200 and verified.json()["status"] == "VERIFIED"
    assert (await client.post(f"{path}/verify", headers=admin)).status_code == 409
    assert (await client.put(path, headers=admin, json={**body, "status": "COMPLETED"})).status_code == 409


async def test_appointment_delete_cancels_and_retains_history(api):
    client, _ = api
    _, admin, _ = await onboard(client)
    department = await create(client, "departments", admin, {"name": "Medicine"})
    doctor = await create(client, "doctors", admin, {"name": "Dr Keep", "department_id": department["id"]})
    room = await create(client, "rooms", admin, {"name": "Room 1"})
    starts = datetime.now(timezone.utc) + timedelta(days=1)
    schedule = await create(client, "schedules", admin, {"doctor_id": doctor["id"], "room_id": room["id"],
        "starts_at": starts.isoformat(), "ends_at": (starts + timedelta(hours=1)).isoformat()})
    patient = await create(client, "patients", admin, {"external_ref": "Keep me"})
    appointment = await create(client, "appointments", admin, {"schedule_id": schedule["id"], "patient_id": patient["id"]})
    path = f"/v1/appointments/{appointment['id']}"
    assert (await client.delete(path, headers=admin)).status_code == 204
    kept = await client.get(path, headers=admin)
    assert kept.status_code == 200 and kept.json()["status"] == "CANCELLED"
    assert (await client.delete(path, headers=admin)).status_code == 409


async def test_outbox_respects_backoff_and_dead_letters_once(api, monkeypatch):
    _, sessions = api
    monkeypatch.setattr(outbox, "SessionLocal", sessions)
    tenant = uuid.uuid4()
    async with sessions() as db:
        due = OutboxEvent(tenant_id=tenant, event_type="token.called", payload={})
        later = OutboxEvent(tenant_id=tenant, event_type="token.called", payload={},
                            available_at=datetime.now(timezone.utc) + timedelta(hours=1))
        db.add_all([due, later])
        await db.commit()
        due_id, later_id = due.id, later.id

    class Failing:
        calls: list = []

        async def publish(self, event):
            self.calls.append(event.id)
            raise RuntimeError("provider down")

    provider = Failing()
    assert await outbox.poll_once(provider, max_attempts=2) == 0
    assert provider.calls == [due_id]  # the not-yet-due event is not touched
    # The failed event is backing off, so an immediate poll does not retry it.
    assert await outbox.poll_once(provider, max_attempts=2) == 0
    assert provider.calls == [due_id]
    async with sessions() as db:
        event = await db.get(OutboxEvent, due_id)
        event.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    await outbox.poll_once(provider, max_attempts=2)
    await outbox.poll_once(provider, max_attempts=2)
    assert provider.calls == [due_id, due_id]
    async with sessions() as db:
        event = await db.get(OutboxEvent, due_id)
        assert event.dead_lettered_at is not None and event.event_type == "token.called"
        assert (await db.get(OutboxEvent, later_id)).attempts == 0


async def test_jwks_is_cached_and_refreshed_for_new_key_ids(monkeypatch):
    requests = []
    keys = [{"kid": "one", "kty": "oct"}]

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"keys": keys})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(auth, "_jwks_cache", {"url": None, "keys": [], "fetched_at": float("-inf")})
    url = "https://identity.example.test/auth/v1/.well-known/jwks.json"
    for _ in range(5):
        assert (await auth.signing_key(url, "one", 600))["kid"] == "one"
    assert len(requests) == 1
    # An unknown kid within the rotation guard window does not hammer the provider.
    with pytest.raises(KeyError):
        await auth.signing_key(url, "two", 600)
    assert len(requests) == 1
    auth._jwks_cache["fetched_at"] -= auth.JWKS_UNKNOWN_KID_REFRESH_SECONDS
    keys.append({"kid": "two", "kty": "oct"})
    assert (await auth.signing_key(url, "two", 600))["kid"] == "two"
    assert len(requests) == 2


def test_schema_revision_matches_alembic_head():
    from pathlib import Path
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from backend.db import SCHEMA_REVISION
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    assert ScriptDirectory.from_config(config).get_current_head() == SCHEMA_REVISION
