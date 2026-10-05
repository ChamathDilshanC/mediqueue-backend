"""Staff-only bed maps and reusable patient stay summaries."""
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .auth import Principal, current_principal, require_role
from .db import get_session
from .entities import READ_ROLES, scope, scoped
from .models import Bed, Branch, Patient, Room, Ward, WardAdmission

router = APIRouter(prefix="/v1", tags=["Ward bed maps"])

def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def stay_summary(admission, zone, now=None):
    """Inclusive calendar days in the branch timezone; discharged stays stop growing."""
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(zone)
    end = aware(admission.discharged_at) if admission.discharged_at else now
    start = aware(admission.admitted_at)
    assigned = aware(admission.bed_assigned_at or admission.admitted_at)
    stay_days = max(1, (end.astimezone(tz).date() - start.astimezone(tz).date()).days + 1)
    bed_days = max(1, (end.astimezone(tz).date() - assigned.astimezone(tz).date()).days + 1)
    planned = admission.planned_discharge_at
    if admission.admission_status != "ADMITTED":
        discharge_state = admission.admission_status
    elif planned:
        difference = (aware(planned).astimezone(tz).date() - now.astimezone(tz).date()).days
        discharge_state = "OVERDUE" if difference < 0 else "DUE_TODAY" if difference == 0 else "SCHEDULED"
    else:
        discharge_state = "NOT_SCHEDULED"
    return {"id": str(admission.id), "status": admission.admission_status, "admitted_at": start,
            "bed_assigned_at": assigned if admission.bed_id else None, "stay_days": stay_days,
            "bed_days": bed_days if admission.bed_id else None, "planned_discharge_at": aware(planned) if planned else None,
            "discharged_at": aware(admission.discharged_at) if admission.discharged_at else None, "discharge_state": discharge_state,
            "assigned_by": admission.assigned_by, "discharged_by": admission.discharged_by}

class StayOutput(BaseModel):
    id: uuid.UUID
    status: str
    admitted_at: datetime
    bed_assigned_at: datetime | None
    stay_days: int
    bed_days: int | None
    planned_discharge_at: datetime | None
    discharged_at: datetime | None
    discharge_state: str
    assigned_by: str
    discharged_by: str
    patient_id: uuid.UUID
    patient_name: str
    mrn: str

class MapBed(BaseModel):
    id: uuid.UUID
    number: str
    type: str
    status: str
    active: bool
    room_id: uuid.UUID | None = None
    room_name: str | None = None
    admission: StayOutput | None

class MapWard(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    type: str
    floor: str
    building: str
    capacity: int

class BedMapOutput(BaseModel):
    ward: MapWard
    timezone: str
    as_of: datetime
    summary: dict[str, int]
    beds: list[MapBed]

@router.get("/wards/{ward_id}/bed-map", response_model=BedMapOutput)
async def bed_map(ward_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    """Return every bed and its current/latest stay only to authorized branch staff."""
    require_role(p, *READ_ROLES)
    ward = await scoped(Ward, ward_id, p, db)
    branch = await db.get(Branch, uuid.UUID(p.branch_id))
    beds = (await db.execute(
        select(Bed, Room.name)
        .outerjoin(Room, Room.id == Bed.room_id)
        .where(*scope(Bed, p), Bed.ward_id == ward.id)
        .order_by(Bed.bed_number, Bed.id)
    )).all()
    rows = (await db.execute(select(WardAdmission, Patient).join(Patient, Patient.id == WardAdmission.patient_id)
                             .where(*scope(WardAdmission, p), WardAdmission.ward_id == ward.id, Patient.tenant_id == ward.tenant_id)
                             .order_by(WardAdmission.admitted_at.desc(), WardAdmission.created_at.desc(), WardAdmission.id))).all()
    latest = {}
    for admission, patient in rows:
        previous = latest.get(admission.bed_id)
        if admission.bed_id and (previous is None or (admission.admission_status == "ADMITTED" and previous[0].admission_status != "ADMITTED")):
            latest[admission.bed_id] = (admission, patient)
    now = datetime.now(timezone.utc)
    summary = {key: 0 for key in ("total", "occupied", "available", "reserved", "cleaning", "maintenance", "inactive", "due_today", "overdue")}
    output = []
    for bed, room_name in beds:
        pair = latest.get(bed.id)
        stay = None
        status = bed.status if bed.is_active else "INACTIVE"
        if pair:
            admission, patient = pair
            stay = {**stay_summary(admission, branch.timezone, now), "patient_id": str(patient.id),
                    "patient_name": " ".join(n for n in (patient.first_name, patient.last_name) if n) or patient.external_ref, "mrn": patient.mrn}
            if admission.admission_status == "ADMITTED":
                status = "OCCUPIED"
                if stay["discharge_state"] == "DUE_TODAY": summary["due_today"] += 1
                if stay["discharge_state"] == "OVERDUE": summary["overdue"] += 1
        summary["total"] += 1
        if status.lower() in summary: summary[status.lower()] += 1
        output.append({"id": str(bed.id), "number": bed.bed_number, "type": bed.bed_type,
                       "status": status, "active": bed.is_active, "room_id": str(bed.room_id) if bed.room_id else None,
                       "room_name": room_name, "admission": stay})
    return {"ward": {"id": str(ward.id), "name": ward.name, "code": ward.ward_code, "type": ward.ward_type,
                     "floor": ward.floor, "building": ward.building, "capacity": ward.bed_capacity},
            "timezone": branch.timezone, "as_of": now, "summary": summary, "beds": output}
