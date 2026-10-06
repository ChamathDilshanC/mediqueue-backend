"""Queue domain tables and constraints owned by the backend service."""
import uuid
from datetime import datetime, date
from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
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
    address: Mapped[str] = mapped_column(String(500), default="", server_default="")
    phone: Mapped[str] = mapped_column(String(40), default="", server_default="")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

class Queue(Base):
    __tablename__ = "queue"; __table_args__ = {"schema": "queue", "sqlite_autoincrement": True}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.department.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    token_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_service_minutes: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    service_type: Mapped[str] = mapped_column(String(30), default="GENERAL", nullable=False)
    room_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.room.id"), nullable=True)

class Patient(Base):
    __tablename__ = "patient"; __table_args__ = {"schema": "queue"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    external_ref: Mapped[str] = mapped_column(String(200))
    mrn: Mapped[str] = mapped_column(String(50), default="")
    first_name: Mapped[str] = mapped_column(String(100), default="")
    last_name: Mapped[str] = mapped_column(String(100), default="")
    gender: Mapped[str] = mapped_column(String(20), default="")
    date_of_birth: Mapped[str] = mapped_column(String(30), default="")
    nic: Mapped[str] = mapped_column(String(30), default="")
    mobile: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    blood_group: Mapped[str] = mapped_column(String(10), default="")
    address_line_1: Mapped[str] = mapped_column(String(200), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    allergies: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")

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
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class StripeWebhookEvent(Base):
    __tablename__ = "stripe_webhook_event"; __table_args__ = {"schema": "notifications"}
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(120))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserProfile(Base):
    __tablename__ = "user_profile"; __table_args__ = {"schema": "iam"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")


class PatientAccount(Base):
    """Verified identity ownership; staff patient IDs are never inferred from email."""
    __tablename__ = "patient_account"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id"), {"schema": "iam"})
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("iam.user_profile.id"), index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("iam.tenant.id"), index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("queue.patient.id"), unique=True)


class ManagementRecord(Base):
    """Validated module payloads with branch scope and optimistic concurrency."""
    __tablename__ = "management_record"
    __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    module: Mapped[str] = mapped_column(String(40), index=True)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("queue.patient.id"), nullable=True)
    data: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    __tablename__ = "membership"
    __table_args__ = (UniqueConstraint("user_id", "branch_id"), {"schema": "iam"})
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("iam.user_profile.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("iam.tenant.id"), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("iam.branch.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class OrganizationApplication(Base):
    __tablename__ = "organization_application"; __table_args__ = {"schema": "iam"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    applicant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("iam.user_profile.id"), index=True)
    organization_type: Mapped[str] = mapped_column(String(30))
    official_name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(String(40))
    official_email: Mapped[str] = mapped_column(String(254))
    registration_number: Mapped[str] = mapped_column(String(120), default="")
    license_number: Mapped[str] = mapped_column(String(120), default="")
    supporting_document_url: Mapped[str] = mapped_column(String(1000), default="")
    website_url: Mapped[str] = mapped_column(String(500), default="")
    administrator_name: Mapped[str] = mapped_column(String(200))
    administrator_role: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="pending_review", index=True)


class Department(Base):
    __tablename__ = "department"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(20), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    head_of_dept: Mapped[str] = mapped_column(String(200), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Room(Base):
    __tablename__ = "room"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.department.id"), nullable=True)
    ward_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.ward.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))


class Doctor(Base):
    __tablename__ = "doctor"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    department_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.department.id"))
    name: Mapped[str] = mapped_column(String(200))
    specialty: Mapped[str] = mapped_column(String(200), default="")
    quotation_template: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")


class Schedule(Base):
    __tablename__ = "schedule"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.doctor.id"))
    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.room.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    capacity: Mapped[int] = mapped_column(Integer, default=20)


class Appointment(Base):
    __tablename__ = "appointment"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    schedule_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.schedule.id"))
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("queue.patient.id"))
    status: Mapped[str] = mapped_column(String(20), default="BOOKED")
    source: Mapped[str] = mapped_column(String(20), default="STAFF", server_default="STAFF")
    review_reason: Mapped[str] = mapped_column(String(500), default="", server_default="")
    reviewed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quotation: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    payment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    payment_status: Mapped[str] = mapped_column(String(20), default="UNPAID", server_default="UNPAID")
    payment_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # The single Stripe Checkout Session allowed to settle this appointment, and the
    # amount (LKR cents) it was created for. Webhooks must match both.
    checkout_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Ward(Base):
    __tablename__ = "ward"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    department_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.department.id"))
    ward_code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    ward_type: Mapped[str] = mapped_column(String(50), default="General")
    floor: Mapped[str] = mapped_column(String(50), default="")
    building: Mapped[str] = mapped_column(String(100), default="")
    gender_type: Mapped[str] = mapped_column(String(20), default="Mixed")
    age_group: Mapped[str] = mapped_column(String(50), default="All")
    bed_capacity: Mapped[int] = mapped_column(Integer, default=30)
    in_charge_staff_id: Mapped[str] = mapped_column(String(200), default="")
    phone_extension: Mapped[str] = mapped_column(String(50), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Bed(Base):
    __tablename__ = "bed"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    ward_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.ward.id"))
    room_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.room.id"), nullable=True)
    bed_number: Mapped[str] = mapped_column(String(50))
    bed_type: Mapped[str] = mapped_column(String(50), default="STANDARD")
    status: Mapped[str] = mapped_column(String(20), default="AVAILABLE")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WardAdmission(Base):
    __tablename__ = "ward_admission"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("queue.patient.id"))
    ward_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.ward.id"))
    bed_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.bed.id"), nullable=True)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    discharged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    planned_discharge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bed_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admission_status: Mapped[str] = mapped_column(String(30), default="ADMITTED")
    assigned_by: Mapped[str] = mapped_column(String(200), default="")
    discharged_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Nurse(Base):
    __tablename__ = "nurse"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    employee_id: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")


class Attendant(Base):
    __tablename__ = "attendant"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    employee_id: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")


class StaffShift(Base):
    __tablename__ = "staff_shift"; __table_args__ = (
        CheckConstraint("(doctor_id IS NOT NULL) + (nurse_id IS NOT NULL) + (attendant_id IS NOT NULL) = 1", name="ck_staff_shift_one_assignee"),
        {"schema": "scheduling"},
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.doctor.id"), nullable=True)
    nurse_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.nurse.id"), nullable=True)
    attendant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.attendant.id"), nullable=True)
    shift_date: Mapped[date] = mapped_column(Date)
    shift: Mapped[str] = mapped_column(String(20))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="SCHEDULED")
    notes: Mapped[str] = mapped_column(String(500), default="")


class StaffAttendance(Base):
    __tablename__ = "staff_attendance"; __table_args__ = {"schema": "scheduling"}
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    shift_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.staff_shift.id"))
    attendance_status: Mapped[str] = mapped_column(String(20), default="PRESENT")
    check_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    check_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(String(500), default="")
    verified_by: Mapped[str] = mapped_column(String(200), default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WardTask(Base):
    __tablename__ = "ward_task"; __table_args__ = (
        CheckConstraint("(doctor_id IS NOT NULL) + (nurse_id IS NOT NULL) + (attendant_id IS NOT NULL) = 1", name="ck_ward_task_one_assignee"),
        {"schema": "scheduling"},
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    ward_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.ward.id"))
    patient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("queue.patient.id"), nullable=True)
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.doctor.id"), nullable=True)
    nurse_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.nurse.id"), nullable=True)
    attendant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduling.attendant.id"), nullable=True)
    task_type: Mapped[str] = mapped_column(String(30))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="ASSIGNED")
    notes: Mapped[str] = mapped_column(Text, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[str] = mapped_column(String(200), default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
