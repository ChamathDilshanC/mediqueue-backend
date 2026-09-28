"""FastAPI application entry point for the MediQueue backend."""
import json
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, Literal
from fastapi import Depends, FastAPI, HTTPException, Header, status
from fastapi.responses import HTMLResponse
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
    database: str = "available"

@app.get("/", tags=["Health"], response_class=HTMLResponse, include_in_schema=False)
async def service_info() -> HTMLResponse:
    """Render a browser-friendly API index with links to every public endpoint."""
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MediQueue API</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, system-ui, sans-serif; }}
    body {{ margin: 0; background: #f4f7fb; color: #172033; }}
    main {{ max-width: 1080px; margin: 36px auto; padding: 0 24px; }}
    .hero, section {{ background: white; border: 1px solid #dce4ef; border-radius: 14px; padding: 26px; margin-bottom: 18px; box-shadow: 0 5px 20px #1720330d; }}
    .hero {{ display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; }}
    h1 {{ margin: 0 0 8px; color: #1261a0; }} h2 {{ margin: 0 0 16px; }}
    p {{ line-height: 1.6; }} a {{ color: #1261a0; font-weight: 600; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .links {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 20px; }}
    .links a {{ background: #1261a0; color: white; padding: 10px 14px; border-radius: 8px; }}
    .status {{ background: #ecfdf3; border: 1px solid #a7e3bf; border-radius: 12px; padding: 16px 20px; min-width: 150px; }}
    .status strong {{ display: block; color: #16804b; font-size: 18px; }} .status span {{ color: #456; font-size: 13px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }}
    .card {{ border: 1px solid #e0e7f0; border-radius: 10px; padding: 16px; }}
    .card h3 {{ margin: 0 0 8px; font-size: 15px; }} .card p {{ margin: 8px 0; font-size: 14px; color: #526176; }}
    .method {{ display: inline-block; border-radius: 5px; padding: 3px 7px; margin-right: 7px; color: white; background: #16804b; font-size: 11px; font-weight: 700; }}
    .post {{ background: #1261a0; }} .auth {{ color: #7a4b00; background: #fff3cd; border-radius: 5px; padding: 3px 7px; font-size: 11px; }}
    code {{ background: #eef3f8; border-radius: 4px; padding: 2px 5px; }}
    .ok {{ color: #16804b; font-weight: 700; }}
    @media (max-width: 680px) {{ .hero {{ display: block; }} .status {{ margin-top: 20px; }} }}
  </style>
</head>
<body>
<main>
  <div class="hero">
    <div><h1>MediQueue API</h1>
    <p>Tenant-scoped healthcare queue and patient-flow backend.</p>
    <p class="ok">Version {app.version} · Live API</p>
    <div class="links">
      <a href="/docs">Swagger UI</a>
      <a href="/redoc">ReDoc</a>
      <a href="/openapi.json">OpenAPI JSON</a>
      <a href="/health">Liveness</a>
      <a href="/health/ready">Database readiness</a>
      <a href="/status">Service status</a>
    </div></div>
    <div class="status"><strong>● Operational</strong><span>API is live</span><br><span>Supabase PostgreSQL</span></div>
  </div>
  <section>
    <h2>System endpoints</h2>
    <div class="grid">
      <div class="card"><h3><a href="/status"><span class="method">GET</span><code>/status</code></a></h3><p>Service and database availability summary.</p><span class="auth">Public</span></div>
      <div class="card"><h3><a href="/health"><span class="method">GET</span><code>/health</code></a></h3><p>Fast API liveness check.</p><span class="auth">Public</span></div>
      <div class="card"><h3><a href="/health/ready"><span class="method">GET</span><code>/health/ready</code></a></h3><p>Runs <code>SELECT 1</code> against PostgreSQL.</p><span class="auth">Public</span></div>
      <div class="card"><h3><a href="/v1/config/public"><span class="method">GET</span><code>/v1/config/public</code></a></h3><p>Allowlisted browser-safe configuration.</p><span class="auth">Public</span></div>
    </div>
  </section>
  <section>
    <h2>Application endpoints</h2>
    <div class="grid">
      <div class="card"><h3><a href="/docs#/Queues/snapshot_v1_queues__queue_id__snapshot_get"><span class="method">GET</span><code>/v1/queues/{{queue_id}}/snapshot</code></a></h3><p>Current non-identifying queue snapshot.</p><span class="auth">Supabase JWT · Staff</span></div>
      <div class="card"><h3><a href="/docs#/Queues/check_in_v1_queues__queue_id__tokens_post"><span class="method post">POST</span><code>/v1/queues/{{queue_id}}/tokens</code></a></h3><p>Check in and create a waiting token.</p><span class="auth">JWT · Reception · Idempotency-Key</span></div>
      <div class="card"><h3><a href="/docs#/Queues/call_next_v1_queues__queue_id__call_next_post"><span class="method post">POST</span><code>/v1/queues/{{queue_id}}/call-next</code></a></h3><p>Call the next waiting token transactionally.</p><span class="auth">JWT · Doctor/Staff · Idempotency-Key</span></div>
      <div class="card"><h3><a href="/docs#/Tokens/transition_v1_tokens__token_id___action__post"><span class="method post">POST</span><code>/v1/tokens/{{token_id}}/{{action}}</code></a></h3><p>Recall, skip, or complete a token.</p><span class="auth">JWT · Authorized role</span></div>
    </div>
  </section>
  <section><h2>Authentication and safety</h2><p>Use the <a href="/docs">interactive API documentation</a> for request schemas, response examples, authorization requirements, idempotency headers, and error responses. Patient-identifying data is not exposed by public display endpoints.</p></section>
</main>
</body>
</html>"""
    )

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
