"""Hospital and medical-center modules with validated, audited scoped records."""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .auth import Principal, current_principal, require_role
from .db import get_session
from .entities import scope, scoped, lock_branch, DEPENDENCIES
from .identity import audit
from .models import ManagementRecord, Patient

router = APIRouter(prefix="/v1", tags=["Management"])

class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_default=True)

class Clinical(Input):
    patient_id: uuid.UUID
    diagnosis: str = Field(min_length=1, max_length=1000)
    notes: str = Field(default="", max_length=5000)
    blood_pressure: str = Field(default="", max_length=30)
    temperature: float | None = Field(default=None, ge=25, le=50)
    status: Literal["DRAFT", "SIGNED"] = "DRAFT"

class Prescription(Input):
    patient_id: uuid.UUID
    medicine: str = Field(min_length=1, max_length=200)
    dosage: str = Field(min_length=1, max_length=200)
    frequency: str = Field(min_length=1, max_length=200)
    duration: str = Field(min_length=1, max_length=100)
    instructions: str = Field(default="", max_length=1000)
    status: Literal["PRESCRIBED", "DISPENSED", "CANCELLED"] = "PRESCRIBED"

class Lab(Input):
    patient_id: uuid.UUID
    test_name: str = Field(min_length=1, max_length=200)
    priority: Literal["ROUTINE", "URGENT"] = "ROUTINE"
    result: str = Field(default="", max_length=5000)
    status: Literal["ORDERED", "COLLECTED", "COMPLETED", "RELEASED", "CANCELLED"] = "ORDERED"

class Invoice(Input):
    patient_id: uuid.UUID
    description: str = Field(min_length=1, max_length=1000)
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    paid_amount: Decimal = Field(default=0, ge=0, max_digits=12, decimal_places=2)
    currency: Literal["LKR"] = "LKR"

class Inventory(Input):
    name: str = Field(min_length=1, max_length=200)
    sku: str = Field(min_length=1, max_length=100)
    category: Literal["MEDICINE", "CONSUMABLE", "EQUIPMENT"] = "MEDICINE"
    quantity: int = Field(ge=0)
    reorder_level: int = Field(default=10, ge=0)
    unit_price: Decimal = Field(default=0, ge=0, max_digits=12, decimal_places=2)
    expiry_date: str = Field(default="", max_length=10, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    supplier: str = Field(default="", max_length=200)

    @field_validator("expiry_date")
    @classmethod
    def valid_expiry(cls, value):
        if value:
            date.fromisoformat(value)
        return value

class Staff(Input):
    name: str = Field(min_length=1, max_length=200)
    designation: str = Field(min_length=1, max_length=200)
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=254)
    shift: Literal["DAY", "NIGHT", "ROTATING"] = "DAY"
    status: Literal["ACTIVE", "ON_LEAVE", "INACTIVE"] = "ACTIVE"

MODULES = {
    "clinical-records": (Clinical, ("admin", "doctor")),
    "prescriptions": (Prescription, ("admin", "doctor")),
    "lab-orders": (Lab, ("admin", "doctor", "staff")),
    "invoices": (Invoice, ("admin", "reception")),
    "inventory": (Inventory, ("admin", "staff")),
    "staff-directory": (Staff, ("admin",)),
}
READ = {
    "clinical-records": ("admin", "doctor"), "prescriptions": ("admin", "doctor", "staff"),
    "lab-orders": ("admin", "doctor", "staff"), "invoices": ("admin", "reception"),
    "inventory": ("admin", "staff"), "staff-directory": ("admin",),
}
DEPENDENCIES[Patient].append((ManagementRecord, ManagementRecord.patient_id))


def output(row):
    return {**row.data, "id": str(row.id), "version": row.version, "created_at": row.created_at,
            "tenant_id": str(row.tenant_id), "branch_id": str(row.branch_id)}


def routes(module, schema, roles):
    update_schema = create_model(f"{schema.__name__}Update", __base__=schema, version=(int, Field(ge=1)))
    output_fields = {"id": (uuid.UUID, ...), "tenant_id": (uuid.UUID, ...), "branch_id": (uuid.UUID, ...),
                     "version": (int, ...), "created_at": (datetime, ...)}
    if module == "invoices":
        output_fields.update(balance=(Decimal, ...), status=(str, ...))
    output_schema = create_model(f"{schema.__name__}Output", __base__=schema, **output_fields)
    async def listing(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session),
                      limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        require_role(p, *READ[module])
        rows = await db.scalars(select(ManagementRecord).where(*scope(ManagementRecord, p), ManagementRecord.module == module)
                                .order_by(ManagementRecord.created_at.desc(), ManagementRecord.id).offset(offset).limit(limit))
        return [output(row) for row in rows]

    async def save(body: dict, p, db, item_id=None):
        require_role(p, *(roles + ("staff",) if item_id and module == "prescriptions" else roles))
        expected_version = body.pop("version", None)
        try:
            parsed = schema.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_context=False)) from exc
        await lock_branch(p, db)
        if module == "inventory":
            duplicate = select(ManagementRecord.id).where(*scope(ManagementRecord, p), ManagementRecord.module == module,
                                                         ManagementRecord.data["sku"].as_string() == parsed.sku)
            if item_id:
                duplicate = duplicate.where(ManagementRecord.id != item_id)
            if await db.scalar(duplicate):
                raise HTTPException(409, "SKU already exists in this branch")
        patient_id = getattr(parsed, "patient_id", None)
        if patient_id:
            await scoped(Patient, patient_id, p, db)
        data = parsed.model_dump(mode="json")
        if module == "invoices":
            if parsed.paid_amount > parsed.amount:
                raise HTTPException(422, "Payment exceeds invoice amount")
            data["balance"] = str(parsed.amount - parsed.paid_amount)
            data["status"] = "PAID" if parsed.paid_amount == parsed.amount else "PARTIAL" if parsed.paid_amount else "UNPAID"
        if module == "lab-orders" and data["status"] in {"COMPLETED", "RELEASED"} and not data["result"]:
            raise HTTPException(422, "A completed lab order needs a result")
        if item_id:
            row = await scoped(ManagementRecord, item_id, p, db, lock=True)
            if row.module != module:
                raise HTTPException(404, "Record not found")
            if expected_version != row.version:
                raise HTTPException(409, "Record changed. Refresh before saving")
            if patient_id != row.patient_id:
                raise HTTPException(409, "Patient ownership cannot be changed")
            if module == "prescriptions" and "staff" in p.roles and not set(p.roles).intersection({"admin", "doctor"}):
                if any(data[k] != row.data.get(k) for k in data if k != "status") or data["status"] != "DISPENSED":
                    raise HTTPException(403, "Staff may only dispense an unchanged prescription")
            if module == "clinical-records" and row.data["status"] == "SIGNED":
                raise HTTPException(409, "Signed clinical records are immutable")
            if module == "invoices" and Decimal(data["paid_amount"]) < Decimal(row.data["paid_amount"]):
                raise HTTPException(409, "Payments cannot be removed")
            if module == "invoices" and Decimal(row.data["paid_amount"]) > 0 and data["amount"] != row.data["amount"]:
                raise HTTPException(409, "An invoice with payments cannot change its amount")
            if module in {"lab-orders", "prescriptions"}:
                transitions = ({"ORDERED": {"COLLECTED", "CANCELLED"}, "COLLECTED": {"COMPLETED", "CANCELLED"}, "COMPLETED": {"RELEASED"}}
                               if module == "lab-orders" else {"PRESCRIBED": {"DISPENSED", "CANCELLED"}})
                before = row.data["status"]
                if data["status"] != before and data["status"] not in transitions.get(before, set()):
                    raise HTTPException(409, "Invalid status transition")
                if before in {"RELEASED", "DISPENSED", "CANCELLED"}:
                    raise HTTPException(409, "Final records are immutable")
            row.data = data
            row.version += 1
        else:
            initial = {"clinical-records": "DRAFT", "prescriptions": "PRESCRIBED", "lab-orders": "ORDERED"}.get(module)
            if initial and data["status"] != initial:
                raise HTTPException(422, f"New records must start as {initial}")
            row = ManagementRecord(tenant_id=uuid.UUID(p.tenant_id), branch_id=uuid.UUID(p.branch_id),
                                   module=module, patient_id=patient_id, data=data)
            db.add(row)
        await db.flush()
        audit(db, p, f"{module}.{'updated' if item_id else 'created'}", row.id)
        await db.commit()
        return output(row)

    async def create(body: schema, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
        return await save(body.model_dump(mode="json"), p, db)

    async def update(item_id: uuid.UUID, body: update_schema, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
        return await save(body.model_dump(mode="json"), p, db, item_id)

    async def detail(item_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
        require_role(p, *READ[module])
        row = await scoped(ManagementRecord, item_id, p, db)
        if row.module != module:
            raise HTTPException(404, "Record not found")
        return output(row)

    router.add_api_route(f"/{module}", listing, methods=["GET"], response_model=list[output_schema], name=f"list_{module}")
    router.add_api_route(f"/{module}/{{item_id}}", detail, methods=["GET"], response_model=output_schema, name=f"get_{module}")
    router.add_api_route(f"/{module}", create, methods=["POST"], response_model=output_schema, status_code=201, name=f"create_{module}")
    router.add_api_route(f"/{module}/{{item_id}}", update, methods=["PUT"], response_model=output_schema, name=f"update_{module}")

for module, (schema, roles) in MODULES.items():
    routes(module, schema, roles)

@router.get("/reports/overview")
async def report(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    from sqlalchemy import func
    from .models import Appointment, Bed, Department, Doctor, Queue, WardAdmission
    require_role(p, "admin", "staff", "doctor", "reception")
    totals = {}
    for key, model in [("patients", Patient), ("doctors", Doctor), ("departments", Department), ("queues", Queue), ("appointments", Appointment), ("beds", Bed)]:
        totals[key] = await db.scalar(select(func.count()).select_from(model).where(*scope(model, p)))
    totals["active_admissions"] = await db.scalar(select(func.count()).select_from(WardAdmission).where(*scope(WardAdmission, p), WardAdmission.admission_status == "ADMITTED"))
    totals["available_beds"] = await db.scalar(select(func.count()).select_from(Bed).where(*scope(Bed, p), Bed.status == "AVAILABLE", Bed.is_active.is_(True)))
    states = (await db.execute(select(Appointment.status, func.count()).where(*scope(Appointment, p)).group_by(Appointment.status))).all()
    result = {"totals": totals, "appointment_statuses": dict(states)}
    if set(p.roles).intersection({"admin", "reception"}):
        invoices = (await db.scalars(select(ManagementRecord).where(*scope(ManagementRecord, p), ManagementRecord.module == "invoices"))).all()
        result["billing"] = {"currency": "LKR", "charged": str(sum((Decimal(r.data["amount"]) for r in invoices), Decimal(0))),
                             "collected": str(sum((Decimal(r.data["paid_amount"]) for r in invoices), Decimal(0))),
                             "outstanding": str(sum((Decimal(r.data["balance"]) for r in invoices), Decimal(0)))}
    if set(p.roles).intersection({"admin", "staff"}):
        stocks = (await db.scalars(select(ManagementRecord).where(*scope(ManagementRecord, p), ManagementRecord.module == "inventory"))).all()
        result["low_stock"] = [{"id": str(r.id), "name": r.data["name"], "quantity": r.data["quantity"], "reorder_level": r.data["reorder_level"]} for r in stocks if r.data["quantity"] <= r.data["reorder_level"]]
    return result
