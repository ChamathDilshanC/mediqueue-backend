"""Queue domain tables and constraints owned by the backend service."""
import uuid
from datetime import datetime, date
from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

def uid() -> uuid.UUID:
    return uuid.uuid4()

class Tenant(Base):
    __tablename__ = "tenant"; __table_args__ = {"schema": "iam"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))

class Branch(Base):
    __tablename__ = "branch"; __table_args__ = {"schema": "iam"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("iam.tenant.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")

class Queue(Base):
    __tablename__ = "queue"; __table_args__ = {"schema": "queue", "sqlite_autoincrement": True}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    token_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

class Patient(Base):
    __tablename__ = "patient"; __table_args__ = {"schema": "queue"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    external_ref: Mapped[str] = mapped_column(String(200))

class Visit(Base):
    __tablename__ = "visit"; __table_args__ = {"schema": "queue"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("queue.patient.id"))

class QueueToken(Base):
    __tablename__ = "queue_token"
    __table_args__ = (UniqueConstraint("tenant_id","branch_id","queue_id","business_date","token_number"),
                      Index("ix_token_waiting","queue_id","status","created_at"), {"schema":"queue"})
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    queue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("queue.queue.id"))
    visit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("queue.visit.id"))
    business_date: Mapped[date] = mapped_column(Date)
    token_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="WAITING", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class IdempotencyKey(Base):
    __tablename__ = "idempotency_key"; __table_args__ = (UniqueConstraint("tenant_id","key"), {"schema":"queue"})
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    key: Mapped[str] = mapped_column(String(200)); result: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict)

class AuditEvent(Base):
    __tablename__ = "audit_event"; __table_args__ = {"schema":"queue"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    actor_id: Mapped[str] = mapped_column(String(200)); action: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True)); payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class OutboxEvent(Base):
    __tablename__ = "outbox_event"; __table_args__ = (Index("ix_outbox_pending","published_at","available_at"), {"schema":"notifications"})
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    event_type: Mapped[str] = mapped_column(String(120)); payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0); available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
