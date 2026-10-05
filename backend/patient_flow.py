"""Owned patient tickets and staff-controlled handoff between care stations."""
import uuid
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .auth import Identity, Principal, current_identity, current_principal, require_role
from .db import get_session
from .entities import lock_branch, scoped
from .models import Queue, QueueToken, Visit, Patient, PatientAccount, Room
from .patient_portal import account
from .schemas import Input
from .queue_estimates import queue_snapshot
from math import ceil

router = APIRouter(prefix="/v1", tags=["Patient journey"])
ACTIVE = ("WAITING", "CALLED", "RECALLED", "IN_SERVICE", "NO_SHOW")

@router.get("/patient/queues/{branch_id}")
async def registration_queues(branch_id: uuid.UUID, identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(Queue).where(Queue.branch_id == branch_id, Queue.service_type == "REGISTRATION").order_by(Queue.name))).all()
    result = []
    for q in rows:
        _, summary = await queue_snapshot(q, db)
        result.append({"id": str(q.id), "name": q.name, **summary})
    return result

@router.get("/patient/queue-status/{branch_id}")
async def center_queue_status(branch_id: uuid.UUID, identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(Queue).where(Queue.branch_id == branch_id).order_by(Queue.name))).all()
    result = []
    for q in rows:
        _, summary = await queue_snapshot(q, db)
        result.append({"id": str(q.id), "name": q.name, "service_type": q.service_type, **summary})
    return result

@router.post("/patient/queues/{queue_id}/tickets", status_code=201)
async def take_ticket(queue_id: uuid.UUID, idempotency_key: str = Header(..., min_length=1, max_length=200), identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    from .main import check_in, CheckIn, business_date, token_result
    q = await db.get(Queue, queue_id)
    if not q or q.service_type != "REGISTRATION":
        raise HTTPException(404, "Registration queue not found")
    link = await account(identity, db, q.tenant_id)
    p = Principal(identity.subject, str(q.tenant_id), str(q.branch_id), ("reception",))
    await lock_branch(p, db)
    existing = await db.scalar(select(QueueToken).join(Visit, Visit.id == QueueToken.visit_id).where(
        Visit.patient_id == link.patient_id, QueueToken.branch_id == q.branch_id,
        QueueToken.business_date == business_date(q), QueueToken.status.in_(ACTIVE)).order_by(QueueToken.created_at.desc()))
    if existing:
        return token_result(existing)
    patient = await db.get(Patient, link.patient_id)
    return await check_in(q.id, CheckIn(patient_ref=patient.external_ref), idempotency_key, p, db)

@router.get("/patient/tickets")
async def my_tickets(identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    from .main import business_date
    ids = select(PatientAccount.patient_id).where(PatientAccount.user_id == uuid.UUID(identity.subject))
    rows = (await db.execute(select(QueueToken, Queue, Room).join(Visit, Visit.id == QueueToken.visit_id)
        .join(Queue, Queue.id == QueueToken.queue_id).outerjoin(Room, Room.id == Queue.room_id)
        .where(Visit.patient_id.in_(ids)).order_by(QueueToken.created_at.desc(), QueueToken.id).limit(100))).all()
    result = []
    snapshots = {}
    for token, q, room in rows:
        today = business_date(q)
        if q.id not in snapshots:
            snapshots[q.id] = await queue_snapshot(q, db)
        current, summary = snapshots[q.id]
        ahead = sum(1 for t in current if t.status == "WAITING" and (t.created_at, t.token_number) < (token.created_at, token.token_number)) if token.status == "WAITING" and token.business_date == today else None
        serving = [f"{t.token_number:03d}" for t in current if t.status in ("CALLED", "RECALLED", "IN_SERVICE")]
        result.append({"id": str(token.id), "visit_id": str(token.visit_id), "label": f"{token.token_number:03d}",
            "status": token.status, "queue": q.name, "stage": q.service_type, "room": room.name if room else q.name,
            "business_date": str(token.business_date), "is_today": token.business_date == today,
            "ahead": ahead, "now_serving": serving, "created_at": token.created_at,
            "waiting_count": summary["waiting_count"], "average_service_minutes": summary["average_service_minutes"],
            "estimate_source": summary["estimate_source"], "as_of": summary["as_of"],
            "estimated_wait_minutes": ceil((ahead + len(serving)) * summary["average_service_minutes"]) if ahead is not None else None})
    return result

class Handoff(Input):
    queue_id: uuid.UUID

@router.post("/journey/tokens/{token_id}/handoff", status_code=201)
async def handoff(token_id: uuid.UUID, body: Handoff, idempotency_key: str = Header(..., min_length=1, max_length=200), p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    from .main import business_date, token_result, command_key, replay, remember, save_event
    require_role(p, "admin", "staff", "reception", "doctor")
    await lock_branch(p, db)
    token = await scoped(QueueToken, token_id, p, db, lock=True)
    key, fingerprint = command_key(p, f"handoff:{token.id}", idempotency_key, body.model_dump(mode="json"))
    previous = await replay(db, token.tenant_id, key, fingerprint)
    if previous is not None:
        return previous
    source = await scoped(Queue, token.queue_id, p, db)
    target = await scoped(Queue, body.queue_id, p, db, lock=True)
    if token.status != "COMPLETED" or token.business_date != business_date(source):
        raise HTTPException(409, "Complete today's current station before issuing the next ticket")
    if target.id == source.id or target.service_type == "REGISTRATION":
        raise HTTPException(422, "Choose a different consultation or onward service queue")
    if "reception" in p.roles and not any(r in p.roles for r in ("admin", "staff", "doctor")) and source.service_type != "REGISTRATION":
        raise HTTPException(403, "Reception can route completed registration tickets only")
    # One onward station per completed token. Persist the link in the audit payload
    # so retries with different keys cannot accidentally create a second handoff.
    from .models import AuditEvent
    existing = (await db.scalars(select(AuditEvent).where(AuditEvent.entity_id == token.id, AuditEvent.action == "journey.handoff"))).first()
    if existing:
        raise HTTPException(409, "This ticket has already been routed to its next station")
    active_station = await db.scalar(select(QueueToken.id).where(QueueToken.visit_id == token.visit_id, QueueToken.status.in_(ACTIVE)))
    if active_station:
        raise HTTPException(409, "Finish the active station before routing this visit")
    target.token_sequence += 1
    next_token = QueueToken(tenant_id=token.tenant_id, branch_id=token.branch_id, queue_id=target.id,
        visit_id=token.visit_id, business_date=business_date(target), token_number=target.token_sequence)
    db.add(next_token)
    await db.flush()
    result = token_result(next_token)
    remember(db, token.tenant_id, key, fingerprint, result)
    await save_event(db, next_token, p, "CHECKED_IN")
    db.add(AuditEvent(tenant_id=token.tenant_id, actor_id=p.subject, action="journey.handoff", entity_id=token.id,
        payload={"next_token_id": str(next_token.id), "queue_id": str(target.id)}))
    await db.commit()
    return result
