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
        if model is WardAdmission and "planned_discharge_at" not in body.model_fields_set:
            data.pop("planned_discharge_at", None)
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
        if model is WardAdmission and "planned_discharge_at" not in body.model_fields_set:
            data.pop("planned_discharge_at", None)
        await validate_entity(model, data, p, db, item_id)
        if model is WardAdmission:
            if item.admission_status != "ADMITTED":
                raise HTTPException(409, "Final admissions are immutable")
            if data["patient_id"] != item.patient_id:
                raise HTTPException(409, "Admission patient cannot be changed")
            if item.bed_id and (item.bed_id != data.get("bed_id") or data["admission_status"] != "ADMITTED"):
                (await scoped(Bed, item.bed_id, p, db)).status = "CLEANING"
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
    """Book against a locked schedule. Reject duplicate active bookings, full or ended sessions."""
    require_role(p, "admin", "staff", "reception")
    await lock_branch(p, db)
    schedule = await scoped(Schedule, body.schedule_id, p, db, lock=True)
    await scoped(Patient, body.patient_id, p, db)
    ends_at = schedule.ends_at.replace(tzinfo=timezone.utc) if schedule.ends_at.tzinfo is None else schedule.ends_at
    if ends_at <= datetime.now(timezone.utc):
        raise HTTPException(409, "Schedule has ended")
    active = [Appointment.schedule_id == schedule.id, Appointment.status != "CANCELLED"]
    if await db.scalar(select(Appointment.id).where(*active, Appointment.patient_id == body.patient_id)):
        raise HTTPException(409, "Patient already has a booking for this schedule")
    count = await db.scalar(select(func.count()).select_from(Appointment).where(*active))
    if count >= schedule.capacity:
        raise HTTPException(409, "Schedule is full")
    row = Appointment(tenant_id=uuid.UUID(p.tenant_id), branch_id=uuid.UUID(p.branch_id), **body.model_dump())
    db.add(row)
    await db.flush()
    audit(db, p, "appointment.booked", row.id)
    await db.commit()
    return row


@router.patch("/appointments/{appointment_id}", tags=["Appointments"], response_model=AppointmentOutput)
async def update_appointment(appointment_id: uuid.UUID, body: AppointmentPatch, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    require_role(p, "admin", "staff", "reception")
    await lock_branch(p, db)
    row = await scoped(Appointment, appointment_id, p, db, lock=True)
    allowed = {"BOOKED": {"CHECKED_IN", "CANCELLED", "NO_SHOW"}, "CHECKED_IN": {"COMPLETED"}}
    if body.status != row.status and body.status not in allowed.get(row.status, set()):
        raise HTTPException(409, "Invalid appointment transition")
    if body.status != row.status:
        row.status = body.status
        audit(db, p, "appointment.updated", row.id)
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
