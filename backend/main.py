"""FastAPI application entry point for the MediQueue backend."""
import json
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, Literal
from fastapi import Depends, FastAPI, HTTPException, Header, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from .auth import Principal, current_principal, require_scope
from .db import get_session
from .models import Queue, QueueToken, Visit, Patient, AuditEvent, OutboxEvent, IdempotencyKey
from .settings import get_settings

app = FastAPI(
    title="MediQueue API",
    summary="Tenant-scoped healthcare queue and patient-flow API",
    description=(
        "MediQueue provides authenticated queue operations for reception and clinical "
        "staff. PostgreSQL is authoritative; queue mutations are transactional, "
        "tenant-scoped, idempotent and audited. Use `/health` for liveness, "
        "`/health/ready` for database readiness, and `/docs` or `/redoc` for the "
        "interactive API reference."
    ),
    version="1.0.0",
    contact={"name": "ChamathDilshanC"},
    license_info={"name": "Proprietary"},
    openapi_tags=[
        {"name": "Health", "description": "Liveness and database readiness checks."},
        {"name": "Configuration", "description": "Allowlisted, browser-safe configuration."},
        {"name": "Queues", "description": "Queue snapshots and token commands."},
        {"name": "Tokens", "description": "Authorized queue-token state transitions."},
    ],
)

class CheckIn(BaseModel):
    patient_ref: str = Field(..., min_length=1, max_length=200, description="Non-clinical patient reference.")

class Transition(BaseModel):
    expected_version: int | None = Field(default=None, ge=1, description="Expected token version for optimistic concurrency.")

class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"

class ReadinessResponse(BaseModel):
    status: Literal["ready"] = "ready"
    database: Literal["ok"] = "ok"

class ErrorResponse(BaseModel):
    detail: str

class TokenResponse(BaseModel):
    id: uuid.UUID
    label: str | None = None
    status: str
    version: int

class QueueSnapshotResponse(BaseModel):
    queueId: uuid.UUID
    tokens: list[TokenResponse]

class PublicConfigResponse(BaseModel):
    schemaVersion: int | None = None
    configVersion: str | None = None
    display: dict[str, Any] | None = None
    public: dict[str, Any] | None = None

@app.get("/health", tags=["Health"], response_model=HealthResponse, summary="Check API liveness")
async def health() -> HealthResponse:
    """Return liveness status without requiring authentication."""
    return HealthResponse()

@app.get(
    "/health/ready",
    tags=["Health"],
    response_model=ReadinessResponse,
    responses={503: {"model": ErrorResponse, "description": "Database is unavailable."}},
    summary="Check API and database readiness",
)
async def readiness(db: AsyncSession = Depends(get_session)) -> ReadinessResponse:
    """Verify that the API can reach its configured PostgreSQL database."""
    try:
        await db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable") from exc
    return ReadinessResponse()

@app.get(
    "/v1/config/public",
    tags=["Configuration"],
    response_model=PublicConfigResponse,
    summary="Fetch browser-safe configuration",
    description="Returns only keys listed in the immutable configuration snapshot's `publicAllowlist`.",
)
async def public_config() -> dict[str, Any]:
    """Return browser-safe allowlisted configuration."""
    with open(get_settings().config_path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {k: data[k] for k in data.get("publicAllowlist", []) if k in data}

async def scoped_queue(queue_id: uuid.UUID, p: Principal, db: AsyncSession) -> Queue:
    q = await db.get(Queue, queue_id)
    if not q: raise HTTPException(404, "Queue not found")
    require_scope(p, str(q.tenant_id), str(q.branch_id)); return q

@app.get(
    "/v1/queues/{queue_id}/snapshot",
    tags=["Queues"],
    response_model=QueueSnapshotResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Get the current queue snapshot",
)
async def snapshot(queue_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)) -> dict:
    """Return non-identifying queue state for an authorized branch."""
    q = await scoped_queue(queue_id, p, db)
    if not any(role in p.roles for role in ("staff", "reception", "admin")):
        raise HTTPException(403, "Role denied")
    rows = (await db.scalars(select(QueueToken).where(QueueToken.queue_id == q.id).order_by(QueueToken.token_number))).all()
    return {"queueId": str(q.id), "tokens": [{"id": str(t.id), "label": f"{t.token_number:03d}", "status": t.status, "version": t.version} for t in rows]}

async def save_event(db, token, p, action):
    db.add(AuditEvent(tenant_id=token.tenant_id, actor_id=p.subject, action=action, entity_id=token.id, payload={"status": token.status}))
    db.add(OutboxEvent(tenant_id=token.tenant_id, event_type=f"token.{action.lower()}", payload={"tokenId": str(token.id), "status": token.status}))

def business_date(queue: Queue) -> date:
    """Calculate the queue business date in its branch timezone."""
    timezone = getattr(queue, "timezone", "UTC")
    return datetime.now(ZoneInfo(timezone)).date()

@app.post(
    "/v1/queues/{queue_id}/tokens",
    tags=["Queues"],
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Check a patient into a queue",
    description="Creates a waiting token. `Idempotency-Key` is required and retries return the original result.",
)
async def check_in(queue_id: uuid.UUID, body: CheckIn, idempotency_key: str = Header(...), p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)) -> dict:
    """Create one waiting token, replaying an idempotent result on retry."""
    q = await scoped_queue(queue_id, p, db)
    if not any(role in p.roles for role in ("staff", "reception", "admin")):
        raise HTTPException(403, "Role denied")
    old = await db.scalar(select(IdempotencyKey).where(IdempotencyKey.tenant_id == q.tenant_id, IdempotencyKey.key == idempotency_key))
    if old: return old.result
    patient = Patient(tenant_id=q.tenant_id, external_ref=body.patient_ref); db.add(patient); await db.flush()
    visit = Visit(tenant_id=q.tenant_id, branch_id=q.branch_id, patient_id=patient.id); db.add(visit); await db.flush()
    # Lock the queue row so concurrent check-ins cannot allocate the same number.
    locked_queue = await db.scalar(select(Queue).where(Queue.id == q.id).with_for_update())
    locked_queue.token_sequence += 1
    token = QueueToken(tenant_id=q.tenant_id, branch_id=q.branch_id, queue_id=q.id, visit_id=visit.id, business_date=business_date(q), token_number=locked_queue.token_sequence); db.add(token); await db.flush()
    result = {"id": str(token.id), "label": f"{token.token_number:03d}", "status": token.status, "version": token.version}
    db.add(IdempotencyKey(tenant_id=q.tenant_id, key=idempotency_key, result=result))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(select(IdempotencyKey).where(IdempotencyKey.tenant_id == q.tenant_id, IdempotencyKey.key == idempotency_key))
        if existing:
            return existing.result
        raise HTTPException(409, "Concurrent idempotency conflict")
    return result

@app.post(
    "/v1/queues/{queue_id}/call-next",
    tags=["Queues"],
    response_model=TokenResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Call the next waiting token",
    description="Selects one eligible waiting token under a database row lock and records audit/outbox events.",
)
async def call_next(queue_id: uuid.UUID, idempotency_key: str = Header(...), p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)) -> dict:
    """Atomically call the oldest waiting token and emit audit/outbox records."""
    q = await scoped_queue(queue_id, p, db)
    if not any(role in p.roles for role in ("staff", "doctor", "admin")):
        raise HTTPException(403, "Role denied")
    old = await db.scalar(select(IdempotencyKey).where(IdempotencyKey.tenant_id == q.tenant_id, IdempotencyKey.key == idempotency_key))
    if old: return old.result
    token = await db.scalar(select(QueueToken).where(QueueToken.queue_id == q.id, QueueToken.status == "WAITING").order_by(QueueToken.created_at).with_for_update())
    if not token: raise HTTPException(409, "No waiting token")
    token.status, token.version = "CALLED", token.version + 1
    result = {"id": str(token.id), "status": token.status, "version": token.version}
    db.add(IdempotencyKey(tenant_id=q.tenant_id, key=idempotency_key, result=result)); await save_event(db, token, p, "CALLED"); await db.commit(); return result

@app.post(
    "/v1/tokens/{token_id}/{action}",
    tags=["Tokens"],
    response_model=TokenResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Apply a token state transition",
    description="Supported actions are `recall`, `skip`, and `complete`. State changes are audited and published to the outbox.",
)
async def transition(token_id: uuid.UUID, action: Literal["recall", "skip", "complete"], body: Transition = Transition(), idempotency_key: str | None = Header(None), p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)) -> dict:
    """Apply an authorized queue state transition and publish its audit event."""
    token = await db.get(QueueToken, token_id)
    if not token: raise HTTPException(404, "Token not found")
    require_scope(p, str(token.tenant_id), str(token.branch_id))
    if not any(role in p.roles for role in ("staff", "doctor", "admin")):
        raise HTTPException(403, "Role denied")
    if idempotency_key:
        old = await db.scalar(select(IdempotencyKey).where(IdempotencyKey.tenant_id == token.tenant_id, IdempotencyKey.key == idempotency_key))
        if old:
            return old.result
    allowed = {"recall": ("CALLED", "RECALLED"), "skip": ("WAITING", "NO_SHOW"), "complete": ("IN_SERVICE", "COMPLETED")}
    allowed["complete"] = ("CALLED", "COMPLETED")
    if action not in allowed or token.status != allowed[action][0] or (body.expected_version is not None and body.expected_version != token.version):
        raise HTTPException(409, "Invalid state transition")
    token.status, token.version = allowed[action][1], token.version + 1
    result = {"id": str(token.id), "status": token.status, "version": token.version}
    if idempotency_key:
        db.add(IdempotencyKey(tenant_id=token.tenant_id, key=idempotency_key, result=result))
    await save_event(db, token, p, action); await db.commit()
    return result
