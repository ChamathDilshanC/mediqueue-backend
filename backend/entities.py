"""Tenant-scoped setup, scheduling, patient and read-only history endpoints."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import Principal, current_principal, require_role
from .db import get_session
from .identity import audit
from .models import (Appointment, AuditEvent, Bed, Branch, Department, Doctor, Patient,
                     Queue, QueueToken, Room, Schedule, Tenant, Visit, Ward, WardAdmission)
from .schemas import (ERROR_RESPONSES, AppointmentInput, AppointmentOutput, AppointmentPatch, AuditOutput,
    BedInput, BedOutput, DepartmentInput, DepartmentOutput, DoctorInput, DoctorOutput, HospitalInput, PatientInput, PatientOutput, QueueInput,
    QueueOutput, RoomInput, RoomOutput, ScheduleInput, ScheduleOutput, ScopedOutput, VisitInput, VisitOutput,
    WardInput, WardOutput, WardAdmissionInput, WardAdmissionOutput)

router = APIRouter(prefix="/v1", responses=ERROR_RESPONSES)
READ_ROLES = ("admin", "staff", "reception", "doctor")


def scope(model, p):
    filters = [model.tenant_id == uuid.UUID(p.tenant_id)]
    if hasattr(model, "branch_id"):
        filters.append(model.branch_id == uuid.UUID(p.branch_id))
    return filters


async def scoped(model, item_id, p, db, lock=False):
    query = select(model).where(model.id == item_id, *scope(model, p))
    if lock:
        query = query.with_for_update()
    row = await db.scalar(query)
    if row is None:
        raise HTTPException(404, f"{model.__name__} not found in this scope")
    return row


async def lock_branch(p, db):
    if not await db.scalar(select(Branch).where(Branch.id == uuid.UUID(p.branch_id),
        Branch.tenant_id == uuid.UUID(p.tenant_id)).with_for_update()):
        raise HTTPException(404, "Branch not found")


async def validate_entity(model, data, p, db, item_id=None):
    if model is Queue:
        if data.get("room_id"):
            await scoped(Room, data["room_id"], p, db)
        if data.get("service_type") == "CONSULTATION" and not data.get("room_id"):
            raise HTTPException(422, "Consultation queues require a doctor room")
    if model is Room and data.get("department_id"):
        await scoped(Department, data["department_id"], p, db)
    elif model is Queue and data.get("department_id"):
        await scoped(Department, data["department_id"], p, db)
    elif model is Doctor:
        await scoped(Department, data["department_id"], p, db)
        quotation = []
        for item in data.get("quotation_template", []):
            if not isinstance(item.get("name"), str) or not item["name"].strip():
                raise HTTPException(422, "Every quotation item needs a name")
            try:
                amount = float(item.get("amount", 0))
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, "Quotation amounts must be numbers") from exc
            if amount < 0:
                raise HTTPException(422, "Quotation amounts cannot be negative")
            quotation.append({"name": item["name"].strip(), "amount": round(amount, 2)})
        data["quotation_template"] = quotation
    elif model is Ward:
        await scoped(Department, data["department_id"], p, db)
    elif model is Bed:
        await scoped(Ward, data["ward_id"], p, db)
        if item_id and await db.scalar(select(WardAdmission.id).where(WardAdmission.bed_id == item_id, WardAdmission.admission_status == "ADMITTED")):
            old = await scoped(Bed, item_id, p, db)
            if data["ward_id"] != old.ward_id or data["status"] != "OCCUPIED" or not data["is_active"]:
                raise HTTPException(409, "An occupied bed cannot be reassigned or made unavailable")
    elif model is WardAdmission:
        old_admission = await db.get(WardAdmission, item_id) if item_id else None
        admitted_at = data.get("admitted_at") or (old_admission.admitted_at if old_admission else datetime.now(timezone.utc))
        if admitted_at.tzinfo is None:
            admitted_at = admitted_at.replace(tzinfo=timezone.utc)
        if item_id and "planned_discharge_at" not in data:
            data["planned_discharge_at"] = old_admission.planned_discharge_at
        if admitted_at > datetime.now(timezone.utc):
            raise HTTPException(422, "Admission date cannot be in the future")
        planned = data.get("planned_discharge_at")
        if planned and (planned.replace(tzinfo=timezone.utc) if planned.tzinfo is None else planned) < admitted_at:
            raise HTTPException(422, "Planned discharge cannot be before admission")
        data["admitted_at"] = admitted_at
        await scoped(Patient, data["patient_id"], p, db)
        await scoped(Ward, data["ward_id"], p, db)
        if data.get("bed_id"):
            bed = await scoped(Bed, data["bed_id"], p, db, lock=True)
            if bed.ward_id != data["ward_id"]:
                raise HTTPException(422, "Bed must belong to the selected ward")
            if data["admission_status"] == "ADMITTED":
                occupied = select(WardAdmission.id).where(*scope(WardAdmission, p), WardAdmission.bed_id == bed.id, WardAdmission.admission_status == "ADMITTED")
                if item_id:
                    occupied = occupied.where(WardAdmission.id != item_id)
                old = await db.get(WardAdmission, item_id) if item_id else None
                if await db.scalar(occupied) or not bed.is_active or (bed.status != "AVAILABLE" and not (old and old.bed_id == bed.id)):
                    raise HTTPException(409, "Bed is unavailable")
        if data["admission_status"] == "ADMITTED":
            active = select(WardAdmission.id).where(*scope(WardAdmission, p), WardAdmission.patient_id == data["patient_id"], WardAdmission.admission_status == "ADMITTED")
            if item_id:
                active = active.where(WardAdmission.id != item_id)
            if await db.scalar(active):
                raise HTTPException(409, "Patient already has an active admission")
        if not item_id and data["admission_status"] != "ADMITTED":
            raise HTTPException(422, "New admissions must start as ADMITTED")
    elif model is Schedule:
        await scoped(Doctor, data["doctor_id"], p, db)
        await scoped(Room, data["room_id"], p, db)
        overlap = select(Schedule.id).where(*scope(Schedule, p),
            or_(Schedule.doctor_id == data["doctor_id"], Schedule.room_id == data["room_id"]),
            Schedule.starts_at < data["ends_at"], Schedule.ends_at > data["starts_at"])
        if item_id:
            overlap = overlap.where(Schedule.id != item_id)
            if await db.scalar(select(Appointment.id).where(Appointment.schedule_id == item_id)):
                raise HTTPException(409, "Schedules with appointment history cannot be edited")
        if await db.scalar(overlap):
            raise HTTPException(409, "Doctor or room already has an overlapping schedule")
    elif model is Patient:
        # Patients are shared inside a hospital; serialize registrations across branches.
        await db.scalar(select(Tenant).where(Tenant.id == uuid.UUID(p.tenant_id)).with_for_update())
        query = select(Patient.id).where(*scope(Patient, p), Patient.external_ref == data["external_ref"])
        if item_id:
            query = query.where(Patient.id != item_id)
        if await db.scalar(query):
            raise HTTPException(409, "Patient reference already exists in this hospital")


@router.post("/ward-admissions/{item_id}/discharge", response_model=WardAdmissionOutput, tags=["Ward-Admissions"])
async def discharge_admission(item_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    require_role(p, "admin", "staff", "reception")
    await lock_branch(p, db)
    admission = await scoped(WardAdmission, item_id, p, db, lock=True)
    if admission.admission_status != "ADMITTED":
        raise HTTPException(409, "Only active admissions can be discharged")
    if admission.bed_id:
        bed = await scoped(Bed, admission.bed_id, p, db, lock=True)
        bed.status = "AVAILABLE"
    admission.admission_status = "DISCHARGED"
    admission.discharged_at = datetime.now(timezone.utc)
    admission.discharged_by = p.subject
    audit(db, p, "ward-admissions.discharged", admission.id)
    await db.commit()
    return admission


DEPENDENCIES = {
    Department: [(Doctor, Doctor.department_id), (Room, Room.department_id), (Ward, Ward.department_id)],
    Room: [(Schedule, Schedule.room_id)],
    Doctor: [(Schedule, Schedule.doctor_id)],
    Ward: [(Bed, Bed.ward_id), (WardAdmission, WardAdmission.ward_id)],
    Bed: [(WardAdmission, WardAdmission.bed_id)],
    Schedule: [(Appointment, Appointment.schedule_id)],
    Patient: [(Visit, Visit.patient_id), (Appointment, Appointment.patient_id), (WardAdmission, WardAdmission.patient_id)],
    Queue: [(QueueToken, QueueToken.queue_id)],
}


def resource_routes(path, model, input_schema, output_schema, write_roles=("admin",), deletable=True):
    """Register uniform typed CRUD contracts with explicit resource validation and scope."""
    tag = path.title()

    async def list_items(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session),
                         limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        require_role(p, *READ_ROLES)
        return (await db.scalars(select(model).where(*scope(model, p)).order_by(model.id).offset(offset).limit(limit))).all()

    async def get_item(item_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
        require_role(p, *READ_ROLES)
        return await scoped(model, item_id, p, db)

    async def create_item(body: input_schema, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
        require_role(p, *write_roles)
        await lock_branch(p, db)
        data = body.model_dump()
        bed_setup = {}
        if model is Ward:
            bed_setup = {key: data.pop(key) for key in ("initial_bed_count", "initial_bed_type", "bed_number_prefix", "bed_start_number")}
        if model is WardAdmission and "planned_discharge_at" not in body.model_fields_set:
            data.pop("planned_discharge_at", None)
        if model is Ward:
            department_data = data.pop("new_department")
            if department_data:
                if not department_data["code"]:
                    department_data["code"] = f"D-{uuid.uuid4().hex[:12]}"
                department = Department(**department_data, tenant_id=uuid.UUID(p.tenant_id), branch_id=uuid.UUID(p.branch_id))
                db.add(department)
                await db.flush()
                data["department_id"] = department.id
                audit(db, p, "departments.created", department.id)
        await validate_entity(model, data, p, db)
        if model is Visit:
            await scoped(Patient, data["patient_id"], p, db)
        data["tenant_id"] = uuid.UUID(p.tenant_id)
        if hasattr(model, "branch_id"):
            data["branch_id"] = uuid.UUID(p.branch_id)
        if model is Queue:
            data["timezone"] = (await db.get(Branch, uuid.UUID(p.branch_id))).timezone
        elif model is Patient and not data.get("mrn"):
            count = await db.scalar(select(func.count()).select_from(Patient).where(Patient.tenant_id == uuid.UUID(p.tenant_id)))
            year = datetime.now(timezone.utc).year
            data["mrn"] = f"MRN-{year}-{(count + 1):06d}"
        elif model is Ward and not data.get("ward_code"):
            count = await db.scalar(select(func.count()).select_from(Ward).where(*scope(Ward, p)))
            data["ward_code"] = f"WARD-{(count + 1):03d}"
        elif model is Bed and not data.get("bed_number"):
            count = await db.scalar(select(func.count()).select_from(Bed).where(*scope(Bed, p)))
            data["bed_number"] = f"BED-{(count + 1):03d}"
        elif model is Department and not data.get("code"):
            count = await db.scalar(select(func.count()).select_from(Department).where(*scope(Department, p)))
            data["code"] = f"DEPT-{(count + 1):02d}"
        item = model(**data)
        if model is WardAdmission and item.bed_id:
            item.bed_assigned_at = item.admitted_at
            (await scoped(Bed, item.bed_id, p, db)).status = "OCCUPIED"
        db.add(item)
        await db.flush()
        if model is Ward:
            for index in range((bed_setup["initial_bed_count"] or item.bed_capacity)):
                bed = Bed(tenant_id=item.tenant_id, branch_id=item.branch_id, ward_id=item.id,
                    bed_number=f'{bed_setup["bed_number_prefix"]}{bed_setup["bed_start_number"] + index:03d}',
                    bed_type=bed_setup["initial_bed_type"], status="AVAILABLE", is_active=True)
                db.add(bed)
                await db.flush()
                audit(db, p, "beds.created", bed.id)
        audit(db, p, f"{path}.created", item.id)
        await db.commit()
        return item

    async def replace_item(item_id: uuid.UUID, body: input_schema, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
        require_role(p, *write_roles)
        await lock_branch(p, db)
        if model is Patient:
            await db.scalar(select(Tenant).where(Tenant.id == uuid.UUID(p.tenant_id)).with_for_update())
        item = await scoped(model, item_id, p, db, lock=True)
        data = body.model_dump()
        bed_setup = {}
        if model is Ward:
            bed_setup = {key: data.pop(key) for key in ("initial_bed_count", "initial_bed_type", "bed_number_prefix", "bed_start_number")}
        if model is WardAdmission and "planned_discharge_at" not in body.model_fields_set:
            data.pop("planned_discharge_at", None)
        if model is Ward:
            if data.pop("new_department") or bed_setup["initial_bed_count"]:
                raise HTTPException(422, "Related setup is only supported when creating a ward")
        await validate_entity(model, data, p, db, item_id)
        if model is WardAdmission:
            if item.admission_status != "ADMITTED":
                raise HTTPException(409, "Final admissions are immutable")
            if data["patient_id"] != item.patient_id:
                raise HTTPException(409, "Admission patient cannot be changed")
            if item.bed_id and (item.bed_id != data.get("bed_id") or data["admission_status"] != "ADMITTED"):
                (await scoped(Bed, item.bed_id, p, db)).status = "AVAILABLE"
            if data.get("bed_id") and data["admission_status"] == "ADMITTED":
                (await scoped(Bed, data["bed_id"], p, db)).status = "OCCUPIED"
                if data["bed_id"] != item.bed_id:
                    item.bed_assigned_at = datetime.now(timezone.utc)
                elif not item.bed_assigned_at or item.bed_assigned_at == item.admitted_at:
                    item.bed_assigned_at = data["admitted_at"]
            if data["admission_status"] != "ADMITTED":
                item.discharged_at = datetime.now(timezone.utc)
        for key, value in data.items():
            setattr(item, key, value)
        audit(db, p, f"{path}.updated", item.id)
        await db.commit()
        return item

    async def delete_item(item_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
        require_role(p, "admin")
        await lock_branch(p, db)
        if model is Patient:
            await db.scalar(select(Tenant).where(Tenant.id == uuid.UUID(p.tenant_id)).with_for_update())
        item = await scoped(model, item_id, p, db, lock=True)
        if model is WardAdmission:
            raise HTTPException(409, "Admission history must be retained; discharge or cancel it instead")
        for child, reference in DEPENDENCIES.get(model, []):
            if await db.scalar(select(child.id).where(reference == item.id)):
                raise HTTPException(409, "Resource has dependent records; retain its history")
        audit(db, p, f"{path}.deleted", item.id)
        await db.delete(item)
        await db.commit()
        return Response(status_code=204)

    list_items.__doc__ = "List records in the selected scope. Staff roles only. Pagination uses limit and offset; patients are shared inside a hospital."
    create_item.__doc__ = f"Create a scoped {model.__name__.lower()}. Allowed roles: {', '.join(write_roles)}. Related IDs must belong to the same scope."
    replace_item.__doc__ = f"Replace editable fields. Allowed roles: {', '.join(write_roles)}. IDs and scope cannot be reassigned."
    delete_item.__doc__ = "Admin only. Delete an unused setup record; dependent records cause 409 and history is retained."
    router.add_api_route(f"/{path}", list_items, methods=["GET"], response_model=list[output_schema], tags=[tag], summary=f"List {path}", name=f"list_{path}")
    router.add_api_route(f"/{path}/{{item_id}}", get_item, methods=["GET"], response_model=output_schema, tags=[tag], summary=f"Get {model.__name__.lower()}", name=f"get_{path}")
    router.add_api_route(f"/{path}", create_item, methods=["POST"], response_model=output_schema, status_code=201, tags=[tag], summary=f"Create {model.__name__.lower()}", name=f"create_{path}")
    if deletable:
        router.add_api_route(f"/{path}/{{item_id}}", replace_item, methods=["PUT"], response_model=output_schema, tags=[tag], summary=f"Update {model.__name__.lower()}", name=f"update_{path}")
        router.add_api_route(f"/{path}/{{item_id}}", delete_item, methods=["DELETE"], status_code=204, tags=[tag], summary=f"Delete unused {model.__name__.lower()}", name=f"delete_{path}")


resource_routes("departments", Department, DepartmentInput, DepartmentOutput)
resource_routes("rooms", Room, RoomInput, RoomOutput)
resource_routes("doctors", Doctor, DoctorInput, DoctorOutput)
resource_routes("wards", Ward, WardInput, WardOutput)
resource_routes("beds", Bed, BedInput, BedOutput)
resource_routes("ward-admissions", WardAdmission, WardAdmissionInput, WardAdmissionOutput, ("admin", "staff", "reception"))
resource_routes("schedules", Schedule, ScheduleInput, ScheduleOutput)
resource_routes("patients", Patient, PatientInput, PatientOutput, ("admin", "staff", "reception"))
resource_routes("queues", Queue, QueueInput, QueueOutput)
resource_routes("visits", Visit, VisitInput, VisitOutput, ("admin", "staff", "reception"), deletable=False)


@router.get("/appointment-inbox", tags=["Appointments"])
async def appointment_inbox(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session),
                            status: str = Query("", pattern="^(|PENDING|BOOKED|REJECTED|CHECKED_IN|COMPLETED|CANCELLED|NO_SHOW)$"),
                            q: str = Query("", max_length=200), limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0)):
    require_role(p, *READ_ROLES)
    base = select(Appointment, Patient, Schedule, Doctor, Room, Branch).join(Patient, Patient.id == Appointment.patient_id).join(
        Schedule, Schedule.id == Appointment.schedule_id).join(Doctor, Doctor.id == Schedule.doctor_id).join(
        Room, Room.id == Schedule.room_id).join(Branch, Branch.id == Appointment.branch_id).where(*scope(Appointment, p))
    counts = dict((await db.execute(select(Appointment.status, func.count()).where(*scope(Appointment, p)).group_by(Appointment.status))).all())
    if status:
        base = base.where(Appointment.status == status)
    if q.strip():
        term = "%" + q.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        base = base.where(or_(Patient.first_name.ilike(term, escape="\\"), Patient.last_name.ilike(term, escape="\\"),
                             Patient.external_ref.ilike(term, escape="\\"), Patient.mrn.ilike(term, escape="\\"), Doctor.name.ilike(term, escape="\\")))
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (await db.execute(base.order_by(Appointment.created_at.desc(), Appointment.id).offset(offset).limit(limit))).all()
    return {"total": total, "counts": counts, "items": [{**AppointmentOutput.model_validate(a).model_dump(),
        "patient_name": (patient.first_name + " " + patient.last_name).strip() or patient.external_ref,
        "patient_ref": patient.mrn or patient.external_ref, "doctor": doctor.name, "room": room.name,
        "starts_at": schedule.starts_at, "ends_at": schedule.ends_at, "timezone": branch.timezone,
        "branch": branch.name} for a, patient, schedule, doctor, room, branch in rows]}


@router.get("/appointments", tags=["Appointments"], response_model=list[AppointmentOutput])
async def appointments(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session),
                       limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    require_role(p, *READ_ROLES)
    return (await db.scalars(select(Appointment).where(*scope(Appointment, p)).order_by(Appointment.created_at, Appointment.id)
                            .offset(offset).limit(limit))).all()


@router.get("/appointments/{appointment_id}", tags=["Appointments"], response_model=AppointmentOutput)
async def appointment(appointment_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    require_role(p, *READ_ROLES)
    return await scoped(Appointment, appointment_id, p, db)


@router.post("/appointments", tags=["Appointments"], response_model=AppointmentOutput, status_code=201)
async def book_appointment(body: AppointmentInput, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    return await create_appointment(body, p, db)


async def create_appointment(body, p, db, patient_requested=False):
    """Book against a locked schedule. Reject duplicate active bookings, full or ended sessions."""
    require_role(p, "admin", "staff", "reception")
    await lock_branch(p, db)
    schedule = await scoped(Schedule, body.schedule_id, p, db, lock=True)
    await scoped(Patient, body.patient_id, p, db)
    doctor = await scoped(Doctor, schedule.doctor_id, p, db)
    ends_at = schedule.ends_at.replace(tzinfo=timezone.utc) if schedule.ends_at.tzinfo is None else schedule.ends_at
    if ends_at <= datetime.now(timezone.utc):
        raise HTTPException(409, "Schedule has ended")
    active = [Appointment.schedule_id == schedule.id, Appointment.status.notin_(("CANCELLED", "REJECTED"))]
    if await db.scalar(select(Appointment.id).where(*active, Appointment.patient_id == body.patient_id)):
        raise HTTPException(409, "Patient already has a booking for this schedule")
    count = await db.scalar(select(func.count()).select_from(Appointment).where(*active))
    if count >= schedule.capacity:
        raise HTTPException(409, "Schedule is full")
    row = Appointment(
        tenant_id=uuid.UUID(p.tenant_id),
        branch_id=uuid.UUID(p.branch_id),
        **body.model_dump(),
        quotation=[*doctor.quotation_template],
        status="PENDING" if patient_requested else "BOOKED",
        source="PATIENT" if patient_requested else "STAFF",
    )
    db.add(row)
    await db.flush()
    audit(db, p, "appointment.requested" if patient_requested else "appointment.booked", row.id)
    await db.commit()
    return row


@router.patch("/appointments/{appointment_id}", tags=["Appointments"], response_model=AppointmentOutput)
async def update_appointment(appointment_id: uuid.UUID, body: AppointmentPatch, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    require_role(p, "admin", "staff", "reception")
    await lock_branch(p, db)
    row = await scoped(Appointment, appointment_id, p, db, lock=True)
    if body.quotation is not None:
        if len(body.quotation) > 50:
            raise HTTPException(422, "A quotation may contain at most 50 items")
        quotation = []
        for item in body.quotation:
            if not isinstance(item.get("name"), str) or not item["name"].strip():
                raise HTTPException(422, "Every quotation item needs a name")
            try:
                amount = float(item.get("amount", 0))
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, "Quotation amounts must be numbers") from exc
            if amount < 0:
                raise HTTPException(422, "Quotation amounts cannot be negative")
            quotation.append({"name": item["name"].strip(), "amount": round(amount, 2)})
        row.quotation = quotation
    allowed = {"PENDING": {"BOOKED", "REJECTED", "CANCELLED"}, "BOOKED": {"CHECKED_IN", "CANCELLED", "NO_SHOW"}, "CHECKED_IN": {"COMPLETED"}}
    if body.status != row.status and body.status not in allowed.get(row.status, set()):
        raise HTTPException(409, "Invalid appointment transition")
    if body.status != row.status:
        if body.status in {"BOOKED", "REJECTED"}:
            if body.status == "REJECTED" and not body.reason.strip():
                raise HTTPException(422, "A rejection reason is required")
            schedule = await scoped(Schedule, row.schedule_id, p, db, lock=True)
            ends_at = schedule.ends_at.replace(tzinfo=timezone.utc) if schedule.ends_at.tzinfo is None else schedule.ends_at
            if body.status == "BOOKED" and ends_at <= datetime.now(timezone.utc):
                raise HTTPException(409, "Cannot approve an ended session")
            row.reviewed_at = datetime.now(timezone.utc)
            row.reviewed_by = p.subject
            row.review_reason = body.reason.strip()
        row.status = body.status
        audit(db, p, f"appointment.{body.status.lower()}", row.id)
        await db.commit()
    elif body.quotation is not None:
        await db.commit()
    return row


@router.delete("/appointments/{appointment_id}", tags=["Appointments"], status_code=204)
async def delete_appointment(appointment_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    require_role(p, "admin", "staff")
    await lock_branch(p, db)
    row = await scoped(Appointment, appointment_id, p, db, lock=True)
    audit(db, p, "appointment.deleted", row.id)
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)


@router.get("/audit-events", tags=["Audit"], response_model=list[AuditOutput])
async def audit_events(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session),
                       limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    """Read immutable audit entries for the selected branch. Legacy unscoped entries stay private."""
    require_role(p, "admin")
    return (await db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == uuid.UUID(p.tenant_id),
        AuditEvent.payload["branch_id"].as_string() == p.branch_id).order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .offset(offset).limit(limit))).all()
