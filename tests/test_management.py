"""Integration coverage for management permissions and patient ownership."""
import uuid
from datetime import datetime, timedelta, timezone
from test_api import api, onboard, create, token

async def test_management_validation_scope_versions_and_payments(api):
    client, _ = api
    _, headers, _ = await onboard(client)
    patient = await create(client, "patients", headers, {"external_ref": "P-1"})
    invoice = await create(client, "invoices", headers, {"patient_id": patient["id"], "description": "Consultation", "amount": "1500.00"})
    assert invoice["balance"] == "1500.00"
    report = (await client.get("/v1/reports/overview", headers=headers)).json()
    assert report["totals"]["patients"] == 1
    assert report["billing"]["outstanding"] == "1500.00"
    body = {"patient_id": patient["id"], "description": "Consultation", "amount": "1500.00", "paid_amount": "500.00", "version": invoice["version"]}
    updated = await client.put(f'/v1/invoices/{invoice["id"]}', headers=headers, json=body)
    assert updated.status_code == 200, updated.text
    assert updated.json()["balance"] == "1000.00"
    assert (await client.put(f'/v1/invoices/{invoice["id"]}', headers=headers, json=body)).status_code == 409
    body.update(version=updated.json()["version"], paid_amount="100.00")
    assert (await client.put(f'/v1/invoices/{invoice["id"]}', headers=headers, json=body)).status_code == 409
    bad = await client.post("/v1/invoices", headers=headers, json={"patient_id": patient["id"], "description": "Bad", "amount": 1, "paid_amount": 2})
    assert bad.status_code == 422
    _, other_headers, _ = await onboard(client, "Other hospital")
    assert (await client.get("/v1/invoices", headers=other_headers)).json() == []
    assert (await client.post("/v1/clinical-records", headers=other_headers, json={"patient_id": patient["id"], "diagnosis": "Test"})).status_code == 404
    assert (await client.post("/v1/inventory", headers=headers, json={"name": "Stock", "sku": "X", "quantity": -1})).status_code == 422

async def test_patient_ownership_booking_and_released_records(api):
    client, _ = api
    _, staff, hospital = await onboard(client)
    patient_headers = {"Authorization": f"Bearer {token(uuid.uuid4())}"}
    stranger = {"Authorization": f"Bearer {token(uuid.uuid4())}"}
    branch = hospital["branch"]["id"]
    enrolled = await client.post("/v1/patient/profiles", headers=patient_headers, json={"branch_id": branch, "full_name": "Patient One", "mobile": "0771234567"})
    assert enrolled.status_code == 201, enrolled.text
    patient_id = enrolled.json()["id"]
    department = await create(client, "departments", staff, {"name": "OPD"})
    room = await create(client, "rooms", staff, {"name": "Consultation"})
    doctor = await create(client, "doctors", staff, {"name": "Doctor", "department_id": department["id"]})
    starts = datetime.now(timezone.utc) + timedelta(days=1)
    schedule = await create(client, "schedules", staff, {"doctor_id": doctor["id"], "room_id": room["id"], "starts_at": starts.isoformat(), "ends_at": (starts + timedelta(hours=1)).isoformat(), "capacity": 1})
    body = {"schedule_id": schedule["id"]}
    assert (await client.post("/v1/patient/appointments", headers=stranger, json=body)).status_code == 403
    booking = await client.post("/v1/patient/appointments", headers=patient_headers, json=body)
    assert booking.status_code == 201, booking.text
    assert (await client.post("/v1/patient/appointments", headers=patient_headers, json=body)).status_code == 409
    assert (await client.patch(f'/v1/patient/appointments/{booking.json()["id"]}/cancel', headers=stranger)).status_code in {403, 404}
    assert (await client.get("/v1/patient/overview", headers=stranger)).json()["appointments"] == []
    clinical = await create(client, "clinical-records", staff, {"patient_id": patient_id, "diagnosis": "Follow-up", "notes": "Private draft"})
    assert (await client.get("/v1/patient/overview", headers=patient_headers)).json()["records"] == []
    signed = await client.put(f'/v1/clinical-records/{clinical["id"]}', headers=staff, json={"patient_id": patient_id, "diagnosis": "Follow-up", "status": "SIGNED", "version": 1})
    assert signed.status_code == 200, signed.text
    assert len((await client.get("/v1/patient/overview", headers=patient_headers)).json()["records"]) == 1
    assert (await client.get("/v1/clinical-records", headers=patient_headers)).status_code == 403
    assert (await client.patch(f'/v1/patient/appointments/{booking.json()["id"]}/cancel', headers=patient_headers)).status_code == 200

async def test_management_roles_and_lab_state_machine(api):
    client, _ = api
    _, admin, hospital = await onboard(client)
    user = uuid.uuid4()
    user_headers = {"Authorization": f"Bearer {token(user)}"}
    await client.get("/v1/auth/me", headers=user_headers)
    await create(client, "memberships", admin, {"user_id": str(user), "role": "reception"})
    user_headers.update({k:v for k,v in admin.items() if k.startswith("X-")})
    assert (await client.get("/v1/clinical-records", headers=user_headers)).status_code == 403
    assert (await client.get("/v1/invoices", headers=user_headers)).status_code == 200
    assert (await client.post("/v1/inventory", headers=user_headers, json={"name": "Stock", "sku": "S", "quantity": 1})).status_code == 403
    patient = await create(client, "patients", admin, {"external_ref": "P-lab"})
    lab = await create(client, "lab-orders", admin, {"patient_id": patient["id"], "test_name": "Test"})
    update = {"patient_id": patient["id"], "test_name": "Test", "status": "RELEASED", "result": "Result", "version": 1}
    assert (await client.put(f'/v1/lab-orders/{lab["id"]}', headers=admin, json=update)).status_code == 409
    for version, status in enumerate(["COLLECTED", "COMPLETED", "RELEASED"], 1):
        update.update(version=version, status=status)
        result = await client.put(f'/v1/lab-orders/{lab["id"]}', headers=admin, json=update)
        assert result.status_code == 200, result.text

async def test_bed_admission_occupancy_and_discharge(api):
    client, _ = api
    _, headers, _ = await onboard(client)
    department = await create(client, "departments", headers, {"name": "Ward department"})
    ward = await create(client, "wards", headers, {"name": "Ward", "department_id": department["id"]})
    bed = await create(client, "beds", headers, {"ward_id": ward["id"]})
    patient = await create(client, "patients", headers, {"external_ref": "Admit-1"})
    other = await create(client, "patients", headers, {"external_ref": "Admit-2"})
    body = {"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"]}
    admission = await create(client, "ward-admissions", headers, body)
    assert (await client.get(f'/v1/beds/{bed["id"]}', headers=headers)).json()["status"] == "OCCUPIED"
    assert (await client.post("/v1/ward-admissions", headers=headers, json={**body, "patient_id": other["id"]})).status_code == 409
    result = await client.put(f'/v1/ward-admissions/{admission["id"]}', headers=headers, json={**body, "admission_status": "DISCHARGED"})
    assert result.status_code == 200, result.text
    assert result.json()["discharged_at"]
    assert (await client.get(f'/v1/beds/{bed["id"]}', headers=headers)).json()["status"] == "AVAILABLE"
    assert (await client.delete(f'/v1/ward-admissions/{admission["id"]}', headers=headers)).status_code == 409

async def test_stock_uniqueness_and_staff_dispensing(api):
    client, _ = api
    _, admin, _ = await onboard(client)
    stock = {"name": "Medicine", "sku": "MED-1", "quantity": 2, "reorder_level": 5}
    await create(client, "inventory", admin, stock)
    assert (await client.post("/v1/inventory", headers=admin, json=stock)).status_code == 409
    assert (await client.post("/v1/inventory", headers=admin, json={**stock, "sku": "MED-2", "expiry_date": "2026-02-31"})).status_code == 422
    assert len((await client.get("/v1/reports/overview", headers=admin)).json()["low_stock"]) == 1
    patient = await create(client, "patients", admin, {"external_ref": "Dispensing patient"})
    body = {"patient_id": patient["id"], "medicine": "Medicine", "dosage": "As prescribed", "frequency": "As prescribed", "duration": "As prescribed"}
    prescription = await create(client, "prescriptions", admin, body)
    user = uuid.uuid4()
    staff = {"Authorization": f"Bearer {token(user)}"}
    await client.get("/v1/auth/me", headers=staff)
    await create(client, "memberships", admin, {"user_id": str(user), "role": "staff"})
    staff.update({k:v for k,v in admin.items() if k.startswith("X-")})
    assert (await client.post("/v1/prescriptions", headers=staff, json=body)).status_code == 403
    path = f'/v1/prescriptions/{prescription["id"]}'
    assert (await client.put(path, headers=staff, json={**body, "medicine": "Changed", "status": "DISPENSED", "version": 1})).status_code == 403
    result = await client.put(path, headers=staff, json={**body, "status": "DISPENSED", "version": 1})
    assert result.status_code == 200, result.text
    assert (await client.put(path, headers=admin, json={**body, "status": "PRESCRIBED", "version": 2})).status_code == 409

async def test_branch_with_management_history_cannot_be_deleted(api):
    client, _ = api
    _, headers, hospital = await onboard(client)
    branch = await create(client, "branches", headers, {"tenant_id": hospital["hospital"]["id"], "name": "Medical center"})
    scoped_headers = {**headers, "X-Branch-ID": branch["id"]}
    await create(client, "staff-directory", scoped_headers, {"name": "Staff member", "designation": "Reception"})
    assert (await client.delete(f'/v1/branches/{branch["id"]}', headers=scoped_headers)).status_code == 409

async def test_capacity_beds_discharge_and_reuse(api):
    client, _ = api
    _, headers, _ = await onboard(client)
    department = await create(client, "departments", headers, {"name": "Medicine"})
    ward = await create(client, "wards", headers, {"name": "Capacity ward", "department_id": department["id"], "bed_capacity": 2})
    snapshot = (await client.get(f'/v1/wards/{ward["id"]}/bed-map', headers=headers)).json()
    assert snapshot["summary"]["available"] == 2
    bed = snapshot["beds"][0]
    patient = await create(client, "patients", headers, {"external_ref": "Discharge patient"})
    body = {"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"]}
    admission = await create(client, "ward-admissions", headers, body)
    result = await client.post(f'/v1/ward-admissions/{admission["id"]}/discharge', headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["admission_status"] == "DISCHARGED"
    assert result.json()["discharged_at"] and result.json()["discharged_by"]
    assert (await client.post(f'/v1/ward-admissions/{admission["id"]}/discharge', headers=headers)).status_code == 409
    other = await create(client, "patients", headers, {"external_ref": "Next patient"})
    await create(client, "ward-admissions", headers, {**body, "patient_id": other["id"]})
    assert (await client.get(f'/v1/ward-admissions/{admission["id"]}', headers=headers)).json()["admission_status"] == "DISCHARGED"
