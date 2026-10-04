"""Bed map dates, discharge states, scope boundaries and service error handling."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid
from backend.models import PatientAccount
from test_api import api, onboard, create, token
from backend.ward_map import stay_summary


def test_stay_days_use_branch_calendar_and_stop_on_discharge():
    now = datetime(2026, 10, 4, 18, tzinfo=timezone.utc)
    admission = SimpleNamespace(id=uuid.uuid4(), admitted_at=datetime(2026, 10, 2, 19, tzinfo=timezone.utc),
        bed_assigned_at=datetime(2026, 10, 4, 4, tzinfo=timezone.utc), bed_id=uuid.uuid4(), discharged_at=None,
        planned_discharge_at=datetime(2026, 10, 4, 12, tzinfo=timezone.utc), admission_status="ADMITTED", assigned_by="Nurse", discharged_by="")
    result = stay_summary(admission, "Asia/Colombo", now)
    assert result["stay_days"] == 2
    assert result["bed_days"] == 1
    assert result["discharge_state"] == "DUE_TODAY"
    admission.planned_discharge_at = now - timedelta(days=1)
    assert stay_summary(admission, "Asia/Colombo", now)["discharge_state"] == "OVERDUE"
    admission.planned_discharge_at = None
    assert stay_summary(admission, "Asia/Colombo", now)["discharge_state"] == "NOT_SCHEDULED"
    admission.bed_assigned_at = admission.admitted_at
    admission.discharged_at = datetime(2026, 10, 3, 4, tzinfo=timezone.utc)
    admission.admission_status = "DISCHARGED"
    result = stay_summary(admission, "Asia/Colombo", now + timedelta(days=30))
    assert result["stay_days"] == 1
    assert result["discharge_state"] == "DISCHARGED"

async def test_bed_map_details_scope_planned_date_and_discharge(api):
    client, _ = api
    _, headers, _ = await onboard(client)
    department = await create(client, "departments", headers, {"name": "General medicine"})
    ward = await create(client, "wards", headers, {"name": "Ward A", "department_id": department["id"]})
    occupied = await create(client, "beds", headers, {"ward_id": ward["id"], "bed_number": "A-01"})
    await create(client, "beds", headers, {"ward_id": ward["id"], "bed_number": "A-02"})
    patient = await create(client, "patients", headers, {"external_ref": "Patient One"})
    now = datetime.now(timezone.utc)
    body = {"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": occupied["id"],
            "admitted_at": (now - timedelta(days=3)).isoformat(), "planned_discharge_at": (now + timedelta(days=1)).isoformat(), "assigned_by": "Nurse"}
    admission = await create(client, "ward-admissions", headers, body)
    path = f'/v1/wards/{ward["id"]}/bed-map'
    response = await client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["summary"]["occupied"] == 1
    assert data["summary"]["available"] == 1
    stay = data["beds"][0]["admission"]
    assert stay["patient_name"] == "Patient One"
    assert stay["stay_days"] == 4 and stay["bed_days"] == 4
    assert stay["discharge_state"] == "SCHEDULED"
    stranger = {"Authorization": f"Bearer {token(uuid.uuid4())}"}
    assert (await client.get(path, headers=stranger)).status_code == 403
    _, other_headers, _ = await onboard(client, "Other center")
    assert (await client.get(path, headers=other_headers)).status_code == 404
    update = {k: v for k, v in body.items() if k != "planned_discharge_at"}
    saved = await client.put(f'/v1/ward-admissions/{admission["id"]}', headers=headers, json=update)
    assert saved.status_code == 200, saved.text
    assert saved.json()["planned_discharge_at"] is not None
    assert (await client.post("/v1/ward-admissions", headers=headers, json={**body, "admitted_at": (now + timedelta(days=1)).isoformat()})).status_code == 422
    discharged = await client.put(f'/v1/ward-admissions/{admission["id"]}', headers=headers, json={**update, "admission_status": "DISCHARGED", "discharged_by": "Doctor"})
    assert discharged.status_code == 200, discharged.text
    data = (await client.get(path, headers=headers)).json()
    assert data["beds"][0]["status"] == "CLEANING"
    assert data["beds"][0]["admission"]["discharged_at"]
    assert data["beds"][0]["admission"]["discharge_state"] == "DISCHARGED"

async def test_patient_only_sees_own_ward_stays(api):
    client, _ = api
    _, staff, hospital = await onboard(client)
    patient_headers = {"Authorization": f"Bearer {token(uuid.uuid4())}"}
    other_headers = {"Authorization": f"Bearer {token(uuid.uuid4())}"}
    profile = await client.post("/v1/patient/profiles", headers=patient_headers, json={"branch_id": hospital["branch"]["id"], "full_name": "Patient One", "mobile": "0771234567"})
    dept = await create(client, "departments", staff, {"name": "Medicine"})
    ward = await create(client, "wards", staff, {"name": "Ward", "department_id": dept["id"]})
    bed = await create(client, "beds", staff, {"ward_id": ward["id"]})
    await create(client, "ward-admissions", staff, {"ward_id": ward["id"], "bed_id": bed["id"], "patient_id": profile.json()["id"]})
    data = (await client.get("/v1/patient/overview", headers=patient_headers)).json()
    assert len(data["ward_stays"]) == 1
    assert data["ward_stays"][0]["bed"] == bed["bed_number"]
    assert (await client.get("/v1/patient/overview", headers=other_headers)).json()["ward_stays"] == []
    assert (await client.get(f'/v1/wards/{ward["id"]}/bed-map', headers=patient_headers)).status_code == 403

async def test_missing_portal_schema_returns_safe_service_error(api):
    client, sessions = api
    async with sessions() as db:
        connection = await db.connection()
        await connection.run_sync(lambda sync: PatientAccount.__table__.drop(sync))
        await db.commit()
    response = await client.get("/v1/patient/overview", headers={"Authorization": f"Bearer {token(uuid.uuid4())}"})
    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"]
    assert "SELECT" not in response.text and "patient_account" not in response.text
