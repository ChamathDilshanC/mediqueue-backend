"""Patient self service using verified identity ownership, independent of staff roles."""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from .auth import Identity, Principal, current_identity
from .db import get_session
from .identity import ensure_profile, audit
from .entities import book_appointment, update_appointment, DEPENDENCIES
from .schemas import AppointmentInput, AppointmentPatch
from .management import Input, output
from .models import Appointment, Bed, Branch, Doctor, ManagementRecord, Patient, PatientAccount, Schedule, Tenant, Ward, WardAdmission
from .ward_map import stay_summary

router = APIRouter(prefix="/v1/patient", tags=["Patient portal"])
DEPENDENCIES[Patient].append((PatientAccount, PatientAccount.patient_id))

class Enrollment(Input):
    branch_id: uuid.UUID
    full_name: str = Field(min_length=1, max_length=200)
    mobile: str = Field(min_length=1, max_length=30)

async def account(identity, db, tenant_id):
    row = await db.scalar(select(PatientAccount).where(PatientAccount.user_id == uuid.UUID(identity.subject), PatientAccount.tenant_id == tenant_id))
    if not row:
        raise HTTPException(403, "Register your patient profile with this hospital first")
    return row

@router.get("/centers")
async def centers(identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(select(Branch, Tenant).join(Tenant, Tenant.id == Branch.tenant_id).order_by(Tenant.name, Branch.name))).all()
    return [{"id": str(b.id), "tenant_id": str(t.id), "name": f"{t.name} · {b.name}",
             "hospital": t.name, "branch": b.name, "address": b.address, "phone": b.phone,
             "latitude": b.latitude, "longitude": b.longitude, "timezone": b.timezone} for b, t in rows]

@router.post("/profiles", status_code=201)
async def enroll(body: Enrollment, identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    profile = await ensure_profile(db, identity)
    # Serialize enrollment per identity, including across branches of one hospital.
    await db.scalar(select(type(profile)).where(type(profile).id == profile.id).with_for_update())
    branch = await db.get(Branch, body.branch_id)
    if not branch:
        raise HTTPException(404, "Center not found")
    existing = await db.scalar(select(PatientAccount).where(PatientAccount.user_id == profile.id, PatientAccount.tenant_id == branch.tenant_id))
    if existing:
        return {"id": str(existing.patient_id)}
    patient = Patient(tenant_id=branch.tenant_id, external_ref=f"{body.full_name[:170]} · P-{profile.id.hex[:12]}",
                      first_name=body.full_name, mobile=body.mobile, email=identity.email,
                      mrn=f"P-{uuid.uuid4().hex[:12].upper()}")
    db.add(patient)
    await db.flush()
    db.add(PatientAccount(user_id=profile.id, tenant_id=branch.tenant_id, patient_id=patient.id))
    audit(db, Principal(identity.subject, str(branch.tenant_id), str(branch.id)), "patient.enrolled", patient.id)
    await db.commit()
    return {"id": str(patient.id)}

@router.get("/overview")
async def overview(identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    links = (await db.scalars(select(PatientAccount).where(PatientAccount.user_id == uuid.UUID(identity.subject)))).all()
    ids = [link.patient_id for link in links]
    profiles = (await db.scalars(select(Patient).where(Patient.id.in_(ids)))).all() if ids else []
    bookings = (await db.execute(select(Appointment, Schedule, Doctor, Branch, Tenant).join(Schedule, Appointment.schedule_id == Schedule.id)
                                .join(Doctor, Schedule.doctor_id == Doctor.id).where(Appointment.patient_id.in_(ids))
                                .join(Branch, Branch.id == Schedule.branch_id).join(Tenant, Tenant.id == Branch.tenant_id)
                                .order_by(Schedule.starts_at.desc()))).all() if ids else []
    records = (await db.scalars(select(ManagementRecord).where(ManagementRecord.patient_id.in_(ids))
                                .order_by(ManagementRecord.created_at.desc()))).all() if ids else []
    visible = [r for r in records if (r.module == "clinical-records" and r.data.get("status") == "SIGNED")
               or (r.module == "lab-orders" and r.data.get("status") == "RELEASED") or r.module in {"prescriptions", "invoices"}]
    stays = (await db.execute(select(WardAdmission, Ward, Bed, Branch).join(Ward, Ward.id == WardAdmission.ward_id)
                             .outerjoin(Bed, Bed.id == WardAdmission.bed_id).join(Branch, Branch.id == WardAdmission.branch_id)
                             .where(WardAdmission.patient_id.in_(ids)).order_by(WardAdmission.admitted_at.desc()))).all() if ids else []
    return {"profiles": [{"id": str(p.id), "name": p.first_name or p.external_ref, "mrn": p.mrn, "tenant_id": str(p.tenant_id)} for p in profiles],
            "ward_stays": [{**stay_summary(a, b.timezone), "ward": w.name, "bed": bed.bed_number if bed else None, "timezone": b.timezone} for a, w, bed, b in stays],
            "appointments": [{"id": str(a.id), "schedule_id": str(s.id), "status": a.status, "doctor": d.name,
                "starts_at": s.starts_at, "ends_at": s.ends_at, "center": f"{t.name} · {b.name}", "timezone": b.timezone,
                "address": b.address, "latitude": b.latitude, "longitude": b.longitude} for a, s, d, b, t in bookings],
            "records": [{**output(r), "module": r.module} for r in visible]}

@router.get("/schedules/{branch_id}")
async def schedules(branch_id: uuid.UUID, identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    from datetime import datetime, timezone
    rows = (await db.execute(select(Schedule, Doctor).join(Doctor, Doctor.id == Schedule.doctor_id)
                            .where(Schedule.branch_id == branch_id, Schedule.ends_at > datetime.now(timezone.utc))
                            .order_by(Schedule.starts_at).limit(200))).all()
    counts = dict((await db.execute(select(Appointment.schedule_id, func.count()).where(
        Appointment.schedule_id.in_([s.id for s, _ in rows]), Appointment.status != "CANCELLED"
    ).group_by(Appointment.schedule_id))).all()) if rows else {}
    links = select(PatientAccount.patient_id).where(PatientAccount.user_id == uuid.UUID(identity.subject))
    booked = set((await db.scalars(select(Appointment.schedule_id).where(
        Appointment.patient_id.in_(links), Appointment.status != "CANCELLED",
        Appointment.schedule_id.in_([s.id for s, _ in rows])))).all()) if rows else set()
    return [{"id": str(s.id), "doctor": d.name, "specialty": d.specialty, "starts_at": s.starts_at,
             "ends_at": s.ends_at, "capacity": s.capacity, "remaining": max(0, s.capacity - counts.get(s.id, 0)),
             "already_booked": s.id in booked} for s, d in rows]

class Booking(Input):
    schedule_id: uuid.UUID

@router.post("/appointments", status_code=201)
async def book(body: Booking, identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    schedule = await db.get(Schedule, body.schedule_id)
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    link = await account(identity, db, schedule.tenant_id)
    # Reuse the transactional staff command only after enforcing identity ownership.
    principal = Principal(identity.subject, str(schedule.tenant_id), str(schedule.branch_id), ("reception",))
    row = await book_appointment(AppointmentInput(schedule_id=schedule.id, patient_id=link.patient_id), principal, db)
    return {"id": str(row.id), "status": row.status}

@router.patch("/appointments/{appointment_id}/cancel")
async def cancel(appointment_id: uuid.UUID, identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    row = await db.get(Appointment, appointment_id)
    if not row:
        raise HTTPException(404, "Appointment not found")
    link = await account(identity, db, row.tenant_id)
    if link.patient_id != row.patient_id:
        raise HTTPException(404, "Appointment not found")
    principal = Principal(identity.subject, str(row.tenant_id), str(row.branch_id), ("reception",))
    result = await update_appointment(row.id, AppointmentPatch(status="CANCELLED"), principal, db)
    return {"id": str(result.id), "status": result.status}
