"""HTTP integration tests: authorization, onboarding, scheduling and queue commands."""
from datetime import datetime, timedelta, timezone
import asyncio
import os
import uuid

import httpx
from jose import jwt
import pytest
from sqlalchemy import event, select, func
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend import auth, identity
from backend.db import Base, get_session
from backend.main import app
from backend.models import AuditEvent, OutboxEvent, Patient, Visit
from backend.settings import Settings

SECRET = "test-only-signing-key-that-is-not-a-deployment-secret"
AUTH_URL = "https://identity.example.test"
SYSTEM_ADMIN_EMAIL = "ops@mediqueue.test"
SYSTEM_ADMIN = uuid.UUID("00000000-0000-4000-8000-00000000a001")


def token(user_id, **overrides):
    claims = {"sub": str(user_id), "aud": "authenticated", "iss": f"{AUTH_URL}/auth/v1",
              "exp": datetime.now(timezone.utc) + timedelta(minutes=10)}
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


@pytest.fixture
async def api(monkeypatch):
    pg_url = os.getenv("MEDIQUEUE_TEST_DATABASE_URL")
    namespaces = ("iam", "queue", "notifications", "scheduling")
    if pg_url:
        suffix = uuid.uuid4().hex[:12]
        schema_map = {name: f"test_{suffix}_{name}" for name in namespaces}
        engine = create_async_engine(pg_url, execution_options={"schema_translate_map": schema_map})
        async with engine.begin() as connection:
            for name in schema_map.values():
                await connection.execute(CreateSchema(name))
    else:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

        @event.listens_for(engine.sync_engine, "connect")
        def attach(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            for schema in namespaces:
                connection.execute(f"ATTACH DATABASE ':memory:' AS {schema}")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def session():
        async with sessions() as db:
            yield db

    settings = Settings(_env_file=None, supabase_url=AUTH_URL, supabase_jwt_secret=SECRET,
                        supabase_anon_key="public-test-key", auth_dev_header_enabled=False,
                        system_admin_emails=f" {SYSTEM_ADMIN_EMAIL.upper()} , other-ops@mediqueue.test")

    async def auth_user(access_token):
        # Stand-in for Supabase GET /auth/v1/user: confirmation comes from the provider record.
        claims = jwt.get_unverified_claims(access_token)
        return {"id": claims["sub"], "email": claims.get("email"),
                "email_confirmed_at": claims.get("test_confirmed_at", "2026-01-01T00:00:00Z")}

    monkeypatch.setattr(auth, "fetch_auth_user", auth_user)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(identity, "get_settings", lambda: settings)
    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, sessions
    app.dependency_overrides.clear()
    if pg_url:
        async with engine.begin() as connection:
            for name in schema_map.values():
                await connection.execute(DropSchema(name, cascade=True))
    await engine.dispose()


def system_admin_headers(**claims):
    return {"Authorization": f"Bearer {token(SYSTEM_ADMIN, email=SYSTEM_ADMIN_EMAIL, **claims)}"}


async def onboard(client, name="Test Hospital"):
    """A verified hospital, provisioned by a system admin for a registered user."""
    user = uuid.uuid4()
    headers = {"Authorization": f"Bearer {token(user)}"}
    assert (await client.get("/v1/auth/me", headers=headers)).status_code == 200
    result = await client.post("/v1/hospitals", headers=system_admin_headers(),
                               json={"name": name, "admin_user_id": str(user)})
    assert result.status_code == 201, result.text
    data = result.json()
    headers.update({"X-Tenant-ID": data["hospital"]["id"], "X-Branch-ID": data["branch"]["id"]})
    return user, headers, data


async def create(client, path, headers, body):
    response = await client.post(f"/v1/{path}", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_registration_rejects_role_injection_and_handles_email_confirmation(api, monkeypatch):
    client, _ = api
    calls = []

    async def provider(method, path, payload=None, token=None):
        calls.append((path, payload))
        return {"id": str(uuid.uuid4()), "email": "test@example.com"}

    monkeypatch.setattr(identity, "auth_request", provider)
    body = {"email": "test@example.com", "password": "test-password", "display_name": "Test User"}
    response = await client.post("/v1/auth/register", json={**body, "role": "admin"})
    assert response.status_code == 422
    assert not calls
    response = await client.post("/v1/auth/register", json=body)
    assert response.status_code == 201
    assert response.json()["confirmation_required"] is True
    assert response.json()["access_token"] is None
    assert calls[0][1]["data"] == {"display_name": "Test User"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("overrides", [{"aud": "other"}, {"iss": "https://attacker.test"}, {"exp": 1}, {"sub": "not-a-uuid"}])
async def test_invalid_tokens_denied(api, overrides):
    client, _ = api
    response = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token(uuid.uuid4(), **overrides)}"})
    assert response.status_code == 401


async def test_no_auth_and_forged_claim_roles_cannot_access_staff_data(api):
    client, _ = api
    assert (await client.get("/v1/patients")).status_code == 401
    forged = token(uuid.uuid4(), roles=["admin"], tenant_id=str(uuid.uuid4()), branch_id=str(uuid.uuid4()))
    assert (await client.get("/v1/patients", headers={"Authorization": f"Bearer {forged}"})).status_code == 403


async def test_auth_provider_session_contract_and_error_redaction(api, monkeypatch):
    client, _ = api
    user = uuid.uuid4()
    real_client = httpx.AsyncClient
    received = []

    def provider(request):
        received.append(request)
        if request.url.path.endswith("/logout"):
            return httpx.Response(204)
        if request.url.path.endswith("/recover"):
            return httpx.Response(500, json={"detail": "private provider diagnostic"})
        return httpx.Response(200, json={"access_token": token(user), "refresh_token": "rotated-test-token",
            "token_type": "bearer", "expires_in": 600, "user": {"id": str(user), "email": "test@example.com"}})

    monkeypatch.setattr(identity.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(provider), **kwargs))
    response = await client.post("/v1/auth/login", json={"email": "test@example.com", "password": "test-password"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert received[-1].url.params["grant_type"] == "password"
    response = await client.post("/v1/auth/refresh", json={"refresh_token": "old-test-token"})
    assert response.json()["refresh_token"] == "rotated-test-token"
    assert received[-1].url.params["grant_type"] == "refresh_token"
    response = await client.post("/v1/auth/logout", headers={"Authorization": f"Bearer {token(user)}"})
    assert response.status_code == 204
    assert received[-1].headers["authorization"].startswith("Bearer ")
    response = await client.post("/v1/auth/forgot-password", json={"email": "test@example.com"})
    assert response.status_code == 503
    assert "private provider diagnostic" not in response.text


async def test_missing_auth_provider_is_explicit_and_profile_metadata_cannot_assign_roles(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(identity, "get_settings", lambda: Settings(_env_file=None, supabase_url=None, supabase_anon_key=None))
    response = await client.post("/v1/auth/register", json={"email": "test@example.com", "password": "test-password", "display_name": "Test"})
    assert response.status_code == 503
    headers = {"Authorization": f"Bearer {token(uuid.uuid4(), user_metadata={'display_name': 'Test Name', 'roles': ['admin']})}"}
    response = await client.get("/v1/auth/me", headers=headers)
    assert response.json()["display_name"] == "Test Name"
    assert response.json()["memberships"] == []


@pytest.mark.parametrize("endpoint,provider_code", [
    ("login", "over_request_rate_limit"),
    ("register", "over_email_send_rate_limit"),
    ("register", "private_unknown_code"),
])
async def test_auth_rate_limits_preserve_status_and_safe_retry_metadata(api, monkeypatch, endpoint, provider_code):
    client, _ = api
    real_client = httpx.AsyncClient

    def provider(request):
        return httpx.Response(429, headers={"Retry-After": "120"},
                              json={"code": provider_code, "msg": "private provider diagnostic"})

    monkeypatch.setattr(identity.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(provider), **kwargs))
    body = {"email": "test@example.com", "password": "test-password"}
    if endpoint == "register":
        body["display_name"] = "Test"
    response = await client.post(f"/v1/auth/{endpoint}", json=body)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "120"
    assert "private provider diagnostic" not in response.text
    if provider_code.startswith("over_"):
        assert response.headers["x-auth-error-code"] == provider_code
    else:
        assert "x-auth-error-code" not in response.headers


async def test_hospital_membership_lifecycle_and_scope(api):
    client, _ = api
    owner, headers, data = await onboard(client)
    account = (await client.get("/v1/auth/me", headers=headers)).json()
    assert account["memberships"][0]["hospital_name"] == data["hospital"]["name"]
    assert account["memberships"][0]["branch_name"] == data["branch"]["name"]
    _, other, other_data = await onboard(client, "Another Hospital")
    assert len((await client.get("/v1/hospitals", headers=headers)).json()) == 1
    response = await client.get(f"/v1/hospitals/{other_data['hospital']['id']}", headers=headers)
    assert response.status_code == 404
    response = await client.patch(f"/v1/memberships/{data['membership']['id']}", headers=headers, json={"role": "staff"})
    assert response.status_code == 409
    user = uuid.uuid4()
    user_headers = {"Authorization": f"Bearer {token(user)}"}
    assert (await client.get("/v1/auth/me", headers=user_headers)).status_code == 200
    member = await create(client, "memberships", headers, {"user_id": str(user), "role": "reception"})
    assert (await client.post("/v1/departments", headers=user_headers, json={"name": "Denied"})).status_code == 403
    assert (await client.get("/v1/users", headers=user_headers)).status_code == 403
    response = await client.patch(f"/v1/memberships/{member['id']}", headers=headers, json={"role": "reception", "active": False})
    assert response.status_code == 200
    assert (await client.get("/v1/patients", headers=user_headers)).status_code == 403
    branch = await create(client, "branches", headers, {"name": "Second Branch"})
    assert (await client.get("/v1/queues", headers={"Authorization": headers["Authorization"]})).status_code == 400
    assert (await client.get("/v1/queues", headers=headers)).status_code == 200
    assert branch["tenant_id"] == data["hospital"]["id"]


async def test_organization_application_persists_verification_fields(api):
    client, _ = api
    user = uuid.uuid4()
    headers = {"Authorization": f"Bearer {token(user)}"}
    response = await client.post(
        "/v1/hospital-applications",
        headers=headers,
        json={
            "organization_type": "medical_center",
            "official_name": "MediQueue Medical Center",
            "address": "123 Main Street, Colombo",
            "phone": "+94112345678",
            "official_email": "admin@mediqueue.example",
            "registration_number": "REG-202536",
            "license_number": "LIC-154121",
            "supporting_document_url": "",
            "website_url": "https://mediqueue.example",
            "administrator_name": "Chamath",
            "administrator_role": "Manager",
        },
    )
    assert response.status_code == 201, response.text
    application = response.json()
    assert application["applicant_id"] == str(user)
    assert application["status"] == "pending_review"
    assert application["organization_type"] == "medical_center"


async def test_entity_crud_relationships_and_branch_isolation(api):
    client, _ = api
    _, headers, _ = await onboard(client)
    _, other, _ = await onboard(client)
    department = await create(client, "departments", headers, {"name": "General Medicine"})
    response = await client.get(f"/v1/departments/{department['id']}", headers=other)
    assert response.status_code == 404
    response = await client.post("/v1/doctors", headers=other, json={"name": "Dr Test", "department_id": department["id"]})
    assert response.status_code == 404
    doctor = await create(client, "doctors", headers, {"name": "Dr Test", "department_id": department["id"]})
    assert (await client.delete(f"/v1/departments/{department['id']}", headers=headers)).status_code == 409
    assert (await client.delete(f"/v1/doctors/{doctor['id']}", headers=headers)).status_code == 204
    assert (await client.delete(f"/v1/departments/{department['id']}", headers=headers)).status_code == 204
    branch = await create(client, "branches", headers, {"name": "Branch 2"})
    second_headers = {**headers, "X-Branch-ID": branch["id"]}
    queue = await create(client, "queues", headers, {"name": "General"})
    assert (await client.get(f"/v1/queues/{queue['id']}", headers=second_headers)).status_code == 404
    assert (await client.get("/v1/queues?limit=201", headers=headers)).status_code == 422
    response = await client.patch(f"/v1/branches/{headers['X-Branch-ID']}", headers=headers, json={"name": "Renamed", "timezone": "UTC"})
    assert response.status_code == 200
    assert (await client.get(f"/v1/queues/{queue['id']}", headers=headers)).json()["timezone"] == "UTC"


async def test_schedule_conflicts_capacity_and_appointment_history(api):
    client, _ = api
    _, headers, _ = await onboard(client)
    department = await create(client, "departments", headers, {"name": "General"})
    doctor = await create(client, "doctors", headers, {"name": "Dr Test", "department_id": department["id"]})
    room = await create(client, "rooms", headers, {"name": "Room 1"})
    start = datetime.now(timezone.utc) + timedelta(days=1)
    body = {"doctor_id": doctor["id"], "room_id": room["id"], "starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=2)).isoformat(), "capacity": 1}
    schedule = await create(client, "schedules", headers, body)
    assert (await client.post("/v1/schedules", headers=headers, json=body)).status_code == 409
    assert (await client.post("/v1/schedules", headers=headers, json={**body, "ends_at": body["starts_at"]})).status_code == 422
    patient = await create(client, "patients", headers, {"external_ref": "PAT-001"})
    second = await create(client, "patients", headers, {"external_ref": "PAT-002"})
    appointment = await create(client, "appointments", headers, {"patient_id": patient["id"], "schedule_id": schedule["id"]})
    response = await client.post("/v1/appointments", headers=headers, json={"patient_id": second["id"], "schedule_id": schedule["id"]})
    assert response.status_code == 409
    assert (await client.delete(f"/v1/schedules/{schedule['id']}", headers=headers)).status_code == 409
    assert (await client.put(f"/v1/schedules/{schedule['id']}", headers=headers, json=body)).status_code == 409
    route = f"/v1/appointments/{appointment['id']}"
    assert (await client.patch(route, headers=headers, json={"status": "COMPLETED"})).status_code == 409
    assert (await client.patch(route, headers=headers, json={"status": "CANCELLED"})).status_code == 200
    assert (await client.patch(route, headers=headers, json={"status": "CHECKED_IN"})).status_code == 409
    await create(client, "appointments", headers, {"patient_id": second["id"], "schedule_id": schedule["id"]})


async def test_queue_idempotency_full_lifecycle_and_audit(api):
    client, sessions = api
    _, headers, _ = await onboard(client)
    queue = await create(client, "queues", headers, {"name": "General"})
    route = f"/v1/queues/{queue['id']}"
    keyed = {**headers, "Idempotency-Key": "repeat-key"}
    first = await client.post(f"{route}/tokens", headers=keyed, json={"patient_ref": "PAT-001"})
    assert first.status_code == 201, first.text
    repeat = await client.post(f"{route}/tokens", headers=keyed, json={"patient_ref": "PAT-001"})
    assert repeat.json() == first.json()
    mismatch = await client.post(f"{route}/tokens", headers=keyed, json={"patient_ref": "PAT-002"})
    assert mismatch.status_code == 409
    called = await client.post(f"{route}/call-next", headers=keyed)
    assert called.json()["status"] == "CALLED"
    assert (await client.post(f"{route}/call-next", headers=keyed)).json() == called.json()
    token_route = f"/v1/tokens/{first.json()['id']}"
    assert (await client.post(f"{token_route}/start", headers=headers, json={"expected_version": 1})).status_code == 409
    started = await client.post(f"{token_route}/start", headers=keyed, json={"expected_version": 2})
    assert started.json()["status"] == "IN_SERVICE"
    assert (await client.post(f"{token_route}/start", headers=keyed, json={"expected_version": 2})).json() == started.json()
    assert (await client.post(f"{token_route}/complete", headers=headers)).json()["status"] == "COMPLETED"
    assert (await client.post(f"{token_route}/cancel", headers=headers)).status_code == 409
    assert (await client.post(f"{route}/call-next", headers={**headers, "Idempotency-Key": "empty"})).status_code == 409
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Patient)) == 1
        assert await db.scalar(select(func.count()).select_from(Visit)) == 1
        assert await db.scalar(select(func.count()).select_from(OutboxEvent)) == 4
    assert len((await client.get("/v1/audit-events", headers=headers)).json()) == 6


async def test_documentation_and_contract(api):
    client, _ = api
    assert (await client.get("/docs")).status_code == 200
    assert (await client.get("/assets/docs.js")).status_code == 200
    contract = (await client.get("/openapi.json")).json()
    assert contract["paths"]["/v1/auth/register"]["post"]["requestBody"]
    assert contract["paths"]["/v1/doctors"]["post"]["security"]
    assert (await client.get("/status")).json()["database"] == "not_checked"


@pytest.mark.skipif(not os.getenv("MEDIQUEUE_TEST_DATABASE_URL"), reason="PostgreSQL row-lock integration test")
async def test_concurrent_queue_commands_do_not_duplicate_tokens(api):
    client, sessions = api
    _, headers, _ = await onboard(client)
    queue = await create(client, "queues", headers, {"name": "Concurrent queue"})
    route = f"/v1/queues/{queue['id']}"
    duplicates = await asyncio.gather(*[client.post(f"{route}/tokens", headers={**headers, "Idempotency-Key": "same"},
        json={"patient_ref": "PAT-SAME"}) for _ in range(5)])
    assert all(r.status_code == 201 for r in duplicates)
    assert len({r.json()["id"] for r in duplicates}) == 1
    entries = await asyncio.gather(*[client.post(f"{route}/tokens", headers={**headers, "Idempotency-Key": f"add-{i}"},
        json={"patient_ref": f"PAT-{i}"}) for i in range(5)])
    assert all(r.status_code == 201 for r in entries)
    assert len({r.json()["label"] for r in entries + duplicates}) == 6
    calls = await asyncio.gather(*[client.post(f"{route}/call-next", headers={**headers, "Idempotency-Key": f"call-{i}"}) for i in range(8)])
    successes = [r for r in calls if r.status_code == 200]
    assert len(successes) == 6
    assert len({r.json()["id"] for r in successes}) == 6
    assert sum(r.status_code == 409 for r in calls) == 2


@pytest.mark.skipif(not os.getenv("MEDIQUEUE_TEST_DATABASE_URL"), reason="PostgreSQL row-lock integration test")
async def test_concurrent_booking_respects_capacity(api):
    client, _ = api
    _, headers, _ = await onboard(client)
    department = await create(client, "departments", headers, {"name": "General"})
    doctor = await create(client, "doctors", headers, {"name": "Dr Test", "department_id": department["id"]})
    room = await create(client, "rooms", headers, {"name": "Room 1"})
    start = datetime.now(timezone.utc) + timedelta(days=1)
    schedule = await create(client, "schedules", headers, {"doctor_id": doctor["id"], "room_id": room["id"],
        "starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat(), "capacity": 1})
    patients = [await create(client, "patients", headers, {"external_ref": f"PAT-{i}"}) for i in range(4)]
    results = await asyncio.gather(*[client.post("/v1/appointments", headers=headers,
        json={"patient_id": patient["id"], "schedule_id": schedule["id"]}) for patient in patients])
    assert sum(r.status_code == 201 for r in results) == 1
    assert sum(r.status_code == 409 for r in results) == 3


async def test_department_full_features_and_branch_management(api):
    client, _ = api
    _, headers, data = await onboard(client)

    # 1. Test Department with full features (code, location, head_of_dept, description, is_active)
    dept_payload = {
        "name": "Cardiology",
        "code": "CARD",
        "location": "Building B, 3rd Floor",
        "head_of_dept": "Dr. A. Perera",
        "description": "Heart and vascular care unit",
        "is_active": True
    }
    dept = await create(client, "departments", headers, dept_payload)
    assert dept["name"] == "Cardiology"
    assert dept["code"] == "CARD"
    assert dept["location"] == "Building B, 3rd Floor"
    assert dept["head_of_dept"] == "Dr. A. Perera"
    assert dept["is_active"] is True

    # Update department
    dept_update = {
        "name": "Cardiology & Vascular",
        "code": "CARD-VASC",
        "location": "Building B, 4th Floor",
        "head_of_dept": "Dr. B. Silva",
        "description": "Expanded heart and vascular center",
        "is_active": True
    }
    resp = await client.put(f"/v1/departments/{dept['id']}", headers=headers, json=dept_update)
    assert resp.status_code == 200, resp.text
    updated_dept = resp.json()
    assert updated_dept["name"] == "Cardiology & Vascular"
    assert updated_dept["code"] == "CARD-VASC"

    # 2. Branch management: cannot delete sole branch of a hospital
    branch_id = data["branch"]["id"]
    del_resp = await client.delete(f"/v1/branches/{branch_id}", headers=headers)
    assert del_resp.status_code == 409
    assert "at least one branch" in del_resp.json()["detail"]

    # Edit branch
    edit_resp = await client.put(f"/v1/branches/{branch_id}", headers=headers, json={"name": "Updated Main Branch", "timezone": "Asia/Colombo"})
    assert edit_resp.status_code == 200
    assert edit_resp.json()["name"] == "Updated Main Branch"
