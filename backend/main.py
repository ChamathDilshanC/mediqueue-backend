"""FastAPI application entry point for the MediQueue backend."""
import hashlib
import json
import logging
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, Literal
from fastapi import Depends, FastAPI, HTTPException, Header, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .identity import router as identity_router
from .entities import router as entities_router, lock_branch
from .portal import router as portal_router
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from .auth import Principal, current_principal, require_scope
from .db import get_session
from .models import Queue, QueueToken, Visit, Patient, Tenant, AuditEvent, OutboxEvent, IdempotencyKey
from .settings import get_settings

logger = logging.getLogger("mediqueue.api")

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
    version="1.1.0",
    docs_url="/swagger",
    contact={"name": "ChamathDilshanC"},
    license_info={"name": "Proprietary"},
    openapi_tags=[
        {"name": "Health", "description": "Liveness and database readiness checks."},
        {"name": "Configuration", "description": "Allowlisted, browser-safe configuration."},
        {"name": "Queues", "description": "Queue snapshots and token commands."},
        {"name": "Tokens", "description": "Authorized queue-token state transitions."},
    ],
)


_openapi = app.openapi


def branded_openapi() -> dict[str, Any]:
    """Add the deployed brand asset to the OpenAPI metadata."""
    schema = _openapi()
    schema["info"]["x-logo"] = {"url": "/assets/brand-logo.png", "altText": "MediQueue"}
    return schema


app.openapi = branded_openapi


class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    patient_ref: str = Field(..., min_length=1, max_length=200, description="Non-clinical patient reference.")

class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")
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

class ServiceInfoResponse(BaseModel):
    name: str
    version: str
    status: Literal["ok"] = "ok"
    documentation: str
    health: str

class StatusResponse(BaseModel):
    service: str
    version: str
    status: Literal["operational"] = "operational"
    database: str = "not_checked"

@app.get("/status", tags=["Health"], response_model=StatusResponse, summary="Get service status")
async def service_status() -> StatusResponse:
    """Return a lightweight public service status without probing the database."""
    return StatusResponse(service=app.title, version=app.version)

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
    except (SQLAlchemyError, OSError, RuntimeError, ValueError) as exc:
        await db.rollback()
        logger.exception("Database readiness check failed")
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
    if not any(role in p.roles for role in ("staff", "reception", "doctor", "admin")):
        raise HTTPException(403, "Role denied")
    rows = (await db.scalars(select(QueueToken).where(QueueToken.queue_id == q.id, QueueToken.business_date == business_date(q)).order_by(QueueToken.token_number))).all()
    return {"queueId": str(q.id), "tokens": [{"id": str(t.id), "label": f"{t.token_number:03d}", "status": t.status, "version": t.version} for t in rows]}

async def save_event(db, token, p, action):
    db.add(AuditEvent(tenant_id=token.tenant_id, actor_id=p.subject, action=action, entity_id=token.id, payload={"status": token.status, "branch_id": str(token.branch_id)}))
    db.add(OutboxEvent(tenant_id=token.tenant_id, event_type=f"token.{action.lower()}", payload={"tokenId": str(token.id), "status": token.status}))


@app.get("/v1/tokens/{token_id}", tags=["Tokens"], response_model=TokenResponse)
async def get_token(token_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    """Read a token's non-identifying state within the selected hospital branch."""
    if not set(p.roles).intersection({"admin", "staff", "doctor", "reception"}):
        raise HTTPException(403, "Role denied")
    token = await db.scalar(select(QueueToken).where(QueueToken.id == token_id,
        QueueToken.tenant_id == uuid.UUID(p.tenant_id), QueueToken.branch_id == uuid.UUID(p.branch_id)))
    if not token:
        raise HTTPException(404, "Token not found")
    return token_result(token)

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
async def check_in(queue_id: uuid.UUID, body: CheckIn, idempotency_key: str = Header(..., min_length=1, max_length=200), p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)) -> dict:
    """Create a patient visit and waiting token in one audited transaction."""
    if not any(role in p.roles for role in ("staff", "reception", "admin")):
        raise HTTPException(403, "Role denied")
    await lock_branch(p, db)
    q = await scoped_queue(queue_id, p, db)
    await db.scalar(select(Tenant).where(Tenant.id == q.tenant_id).with_for_update())
    locked_queue = await db.scalar(select(Queue).where(Queue.id == q.id).with_for_update())
    key, fingerprint = command_key(p, f"check-in:{q.id}", idempotency_key, body.model_dump())
    previous = await replay(db, q.tenant_id, key, fingerprint)
    if previous is not None:
        return previous
    patient = await db.scalar(select(Patient).where(Patient.tenant_id == q.tenant_id, Patient.external_ref == body.patient_ref))
    if patient is None:
        patient = Patient(tenant_id=q.tenant_id, external_ref=body.patient_ref)
        db.add(patient)
        await db.flush()
    visit = Visit(tenant_id=q.tenant_id, branch_id=q.branch_id, patient_id=patient.id)
    db.add(visit)
    await db.flush()
    locked_queue.token_sequence += 1
    token = QueueToken(tenant_id=q.tenant_id, branch_id=q.branch_id, queue_id=q.id, visit_id=visit.id,
                       business_date=business_date(q), token_number=locked_queue.token_sequence)
    db.add(token)
    await db.flush()
    result = token_result(token)
    remember(db, q.tenant_id, key, fingerprint, result)
    await save_event(db, token, p, "CHECKED_IN")
    await db.commit()
    return result

@app.post(
    "/v1/queues/{queue_id}/call-next",
    tags=["Queues"],
    response_model=TokenResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Call the next waiting token",
    description="Selects one eligible waiting token under a database row lock and records audit/outbox events.",
)
async def call_next(queue_id: uuid.UUID, idempotency_key: str = Header(..., min_length=1, max_length=200), p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)) -> dict:
    if not any(role in p.roles for role in ("staff", "doctor", "admin")):
        raise HTTPException(403, "Role denied")
    await lock_branch(p, db)
    q = await scoped_queue(queue_id, p, db)
    await db.scalar(select(Queue).where(Queue.id == q.id).with_for_update())
    key, fingerprint = command_key(p, f"call-next:{q.id}", idempotency_key, {})
    previous = await replay(db, q.tenant_id, key, fingerprint)
    if previous is not None:
        return previous
    token = await db.scalar(select(QueueToken).where(QueueToken.queue_id == q.id, QueueToken.status == "WAITING",
        QueueToken.business_date == business_date(q)).order_by(QueueToken.created_at, QueueToken.token_number).limit(1).with_for_update())
    if not token:
        raise HTTPException(409, "No waiting token")
    token.status, token.version = "CALLED", token.version + 1
    result = token_result(token)
    remember(db, q.tenant_id, key, fingerprint, result)
    await save_event(db, token, p, "CALLED")
    await db.commit()
    return result

@app.post(
    "/v1/tokens/{token_id}/{action}",
    tags=["Tokens"],
    response_model=TokenResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Apply a token state transition",
    description="Supported actions are `recall`, `skip`, `start`, `complete`, and `cancel`. State changes are audited and published to the outbox.",
)
async def transition(token_id: uuid.UUID, action: Literal["recall", "skip", "start", "complete", "cancel"], body: Transition = Transition(), idempotency_key: str | None = Header(None, min_length=1, max_length=200), p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)) -> dict:
    if not any(role in p.roles for role in ("staff", "doctor", "admin")):
        raise HTTPException(403, "Role denied")
    await lock_branch(p, db)
    token = await db.scalar(select(QueueToken).where(QueueToken.id == token_id).with_for_update())
    if not token:
        raise HTTPException(404, "Token not found")
    require_scope(p, str(token.tenant_id), str(token.branch_id))
    if idempotency_key:
        key, fingerprint = command_key(p, f"{action}:{token_id}", idempotency_key, body.model_dump())
        previous = await replay(db, token.tenant_id, key, fingerprint)
        if previous is not None:
            return previous
    allowed = {
        "recall": ({"CALLED", "RECALLED", "NO_SHOW"}, "RECALLED"),
        "skip": ({"WAITING", "CALLED", "RECALLED"}, "NO_SHOW"),
        "start": ({"CALLED", "RECALLED"}, "IN_SERVICE"),
        "complete": ({"CALLED", "RECALLED", "IN_SERVICE"}, "COMPLETED"),
        "cancel": ({"WAITING", "CALLED", "RECALLED", "NO_SHOW"}, "CANCELLED"),
    }
    source, target = allowed[action]
    if token.status not in source or (body.expected_version is not None and body.expected_version != token.version):
        raise HTTPException(409, "Invalid state transition or stale version")
    token.status, token.version = target, token.version + 1
    result = token_result(token)
    if idempotency_key:
        remember(db, token.tenant_id, key, fingerprint, result)
    await save_event(db, token, p, action)
    await db.commit()
    return result


def token_result(token):
    return {"id": str(token.id), "label": f"{token.token_number:03d}", "status": token.status, "version": token.version}


def command_key(p, operation, key, payload):
    scoped_key = hashlib.sha256(f"{p.subject}:{p.branch_id}:{operation}:{key}".encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return scoped_key, fingerprint


async def replay(db, tenant_id, key, fingerprint):
    previous = await db.scalar(select(IdempotencyKey).where(IdempotencyKey.tenant_id == tenant_id, IdempotencyKey.key == key))
    if previous is None:
        return None
    if previous.result.get("fingerprint") != fingerprint:
        raise HTTPException(409, "Idempotency-Key was already used with a different request")
    return previous.result["response"]


def remember(db, tenant_id, key, fingerprint, result):
    db.add(IdempotencyKey(tenant_id=tenant_id, key=key, result={"fingerprint": fingerprint, "response": result}))


@app.exception_handler(IntegrityError)
async def constraint_conflict(request, exc):
    return JSONResponse(status_code=409, content={"detail": "Record conflicts with existing data or dependent records"})


app.include_router(identity_router)
app.include_router(entities_router)
app.include_router(portal_router)
app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "static"), name="assets")
