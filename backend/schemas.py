"""Strict input contracts shared by the identity and entity APIs."""
import uuid
import re
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Role = Literal["admin", "staff", "reception", "doctor"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ApiError(Output):
    detail: str


ERROR_RESPONSES = {
    400: {"model": ApiError, "description": "Invalid request or missing branch selection."},
    401: {"model": ApiError, "description": "Missing, invalid or expired access token."},
    403: {"model": ApiError, "description": "Role or membership does not permit this operation."},
    404: {"model": ApiError, "description": "Resource not found in the selected scope."},
    409: {"model": ApiError, "description": "Conflict, dependent records, capacity or state constraint."},
    429: {"model": ApiError, "description": "Identity provider rate limit reached."},
    503: {"model": ApiError, "description": "Required service is unavailable or not configured."},
}


class Credentials(Input):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=8, max_length=128)


class Register(Credentials):
    display_name: Name


class Refresh(Input):
    refresh_token: str = Field(min_length=1, max_length=4096)


class Recover(Input):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class PasswordChange(Input):
    password: str = Field(min_length=8, max_length=128)


class AuthUser(Output):
    id: uuid.UUID
    email: str | None = None


class AuthResult(Output):
    user: AuthUser | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None
    confirmation_required: bool = False


class ProfileInput(Input):
    display_name: Name


class ProfileOutput(Output):
    id: uuid.UUID
    display_name: str


class MembershipOutput(Output):
    id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    branch_id: uuid.UUID
    role: Role
    active: bool


class AccountMembershipOutput(MembershipOutput):
    hospital_name: str
    branch_name: str


class MeOutput(ProfileOutput):
    memberships: list[AccountMembershipOutput]


class HospitalInput(Input):
    name: Name


class BranchInput(HospitalInput):
    timezone: str = Field(default="Asia/Colombo", max_length=64)
    address: str = Field(default="", max_length=500)
    phone: str = Field(default="", max_length=40)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)

    @model_validator(mode="after")
    def coordinate_pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Provide both latitude and longitude, or leave both blank")
        return self

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA timezone") from exc
        return value

class BranchCreateInput(BranchInput):
    tenant_id: uuid.UUID | None = None


class HospitalOutput(Output):
    id: uuid.UUID
    name: str


class BranchOutput(HospitalOutput):
    tenant_id: uuid.UUID
    timezone: str
    address: str = ""
    phone: str = ""
    latitude: float | None = None
    longitude: float | None = None


class HospitalRegistration(Output):
    hospital: HospitalOutput
    branch: BranchOutput
    membership: MembershipOutput


class MembershipInput(Input):
    user_id: uuid.UUID
    role: Role


class MembershipPatch(Input):
    role: Role
    active: bool = True


class HospitalRegister(BranchInput):
    branch_name: Name = "Main branch"


class OrganizationApplicationInput(Input):
    organization_type: Literal["hospital", "medical_center"]
    official_name: Name
    address: str = Field(min_length=5, max_length=1000)
    phone: str = Field(min_length=7, max_length=40)
    official_email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    registration_number: str = Field(default="", max_length=120)
    license_number: str = Field(default="", max_length=120)
    supporting_document_url: str = Field(default="", max_length=1000)
    website_url: str = Field(default="", max_length=500)
    administrator_name: Name
    administrator_role: Name


class OrganizationApplicationOutput(OrganizationApplicationInput, Output):
    id: uuid.UUID
    applicant_id: uuid.UUID
    status: Literal["pending_review", "verified", "rejected"]


class AdminApplicationPatch(Input):
    status: Literal["verified", "rejected"]


class ScopedOutput(HospitalOutput):
    tenant_id: uuid.UUID
    branch_id: uuid.UUID


class DepartmentInput(Input):
    name: Name
    code: str = Field(default="", max_length=20)
    description: str = Field(default="", max_length=500)
    location: str = Field(default="", max_length=200)
    head_of_dept: str = Field(default="", max_length=200)
    is_active: bool = Field(default=True)


class DepartmentOutput(ScopedOutput):
    name: str
    code: str = ""
    description: str = ""
    location: str = ""
    head_of_dept: str = ""
    is_active: bool = True

    @field_validator("code", "description", "location", "head_of_dept", mode="before")
    @classmethod
    def default_string(cls, v):
        return "" if v is None else v

    @field_validator("is_active", mode="before")
    @classmethod
    def default_bool(cls, v):
        return True if v is None else v


class DoctorInput(HospitalInput):
    department_id: uuid.UUID
    specialty: str = Field(default="", max_length=200)
    quotation_template: list[dict] = Field(default_factory=list, max_length=50)


class DoctorOutput(ScopedOutput):
    department_id: uuid.UUID
    specialty: str
    quotation_template: list[dict] = []


class ScheduleInput(Input):
    doctor_id: uuid.UUID
    room_id: uuid.UUID
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    capacity: int = Field(default=20, ge=1, le=1000)

    @model_validator(mode="after")
    def ordered(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class ScheduleOutput(Output):
    id: uuid.UUID
    tenant_id: uuid.UUID
    branch_id: uuid.UUID
    doctor_id: uuid.UUID
    room_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    capacity: int


class PatientInput(Input):
    external_ref: Name
    mrn: str = Field(default="", max_length=50)
    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    gender: str = Field(default="", max_length=20)
    date_of_birth: str = Field(default="", max_length=30)
    nic: str = Field(default="", max_length=30)
    mobile: str = Field(default="", max_length=30)
    email: str = Field(default="", max_length=254)
    blood_group: str = Field(default="", max_length=10)
    address_line_1: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=100)
    allergies: str = Field(default="", max_length=500)
    status: Literal["ACTIVE", "INACTIVE", "DECEASED", "ARCHIVED"] = "ACTIVE"

    @field_validator("mobile")
    @classmethod
    def valid_mobile(cls, value):
        value = re.sub(r"[\s-]", "", value)
        if value.startswith("+94"):
            value = "0" + value[3:]
        if not re.fullmatch(r"07\d{8}", value):
            raise ValueError("Use a valid Sri Lankan mobile number (07XXXXXXXX)")
        return value

    @field_validator("nic")
    @classmethod
    def valid_nic(cls, value):
        value = value.strip().upper()
        if value and not re.fullmatch(r"(?:\d{9}[VX]|\d{12})", value):
            raise ValueError("Use a valid NIC (9 digits with V/X or 12 digits)")
        return value


class PatientOutput(Output):
    id: uuid.UUID
    tenant_id: uuid.UUID
    external_ref: str
    mrn: str = ""
    first_name: str = ""
    last_name: str = ""
    gender: str = ""
    date_of_birth: str = ""
    nic: str = ""
    mobile: str = ""
    email: str = ""
    blood_group: str = ""
    address_line_1: str = ""
    city: str = ""
    allergies: str = ""
    status: str = "ACTIVE"


class RoomInput(HospitalInput):
    department_id: uuid.UUID | None = None
    ward_id: uuid.UUID | None = None


class RoomOutput(ScopedOutput):
    department_id: uuid.UUID | None = None
    ward_id: uuid.UUID | None = None


class QueueInput(Input):
    average_service_minutes: int = Field(default=5, ge=1, le=120)
    name: str = Field(min_length=1, max_length=120)
    department_id: uuid.UUID | None = None
    service_type: Literal["GENERAL", "REGISTRATION", "CONSULTATION", "DISPENSARY"] = "GENERAL"
    room_id: uuid.UUID | None = None


class QueueOutput(ScopedOutput):
    average_service_minutes: int = 5
    department_id: uuid.UUID | None = None
    service_type: str
    room_id: uuid.UUID | None = None
    timezone: str
    token_sequence: int


class VisitInput(Input):
    patient_id: uuid.UUID


class VisitOutput(Output):
    id: uuid.UUID
    tenant_id: uuid.UUID
    branch_id: uuid.UUID
    patient_id: uuid.UUID


class AppointmentInput(VisitInput):
    schedule_id: uuid.UUID


class AppointmentPatch(Input):
    status: Literal["BOOKED", "REJECTED", "CHECKED_IN", "CANCELLED", "COMPLETED", "NO_SHOW"]
    reason: str = Field(default="", max_length=500)
    quotation: list[dict] | None = None


class AppointmentOutput(VisitOutput):
    source: str = "STAFF"
    review_reason: str = ""
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    quotation: list[dict] = []
    payment_method: str | None = None
    payment_status: str = "UNPAID"
    payment_reference: str | None = None
    schedule_id: uuid.UUID
    status: str
    created_at: datetime


class AuditOutput(Output):
    id: uuid.UUID
    tenant_id: uuid.UUID
    actor_id: str
    action: str
    entity_id: uuid.UUID
    payload: dict
    created_at: datetime


class WardInput(Input):
    new_department: DepartmentInput | None = None
    initial_bed_count: int = Field(default=0, ge=0, le=1000)
    initial_bed_type: Literal["STANDARD", "ICU", "ISOLATION", "PEDIATRIC", "MATERNITY"] = "STANDARD"
    bed_number_prefix: str = Field(default="BED-", max_length=30)
    bed_start_number: int = Field(default=1, ge=1, le=999999)

    @model_validator(mode="after")
    def validate_initial_beds(self):
        if bool(self.department_id) == bool(self.new_department):
            raise ValueError("Select an existing department or provide a new department")
        if self.initial_bed_count > self.bed_capacity:
            raise ValueError("Initial beds cannot exceed ward capacity")
        return self

    department_id: uuid.UUID | None = None
    ward_code: str = Field(default="", max_length=50)
    name: Name
    ward_type: str = Field(default="General", max_length=50)
    floor: str = Field(default="", max_length=50)
    building: str = Field(default="", max_length=100)
    gender_type: Literal["Male", "Female", "Mixed"] = "Mixed"
    age_group: str = Field(default="All", max_length=50)
    bed_capacity: int = Field(default=30, ge=1, le=1000)
    in_charge_staff_id: str = Field(default="", max_length=200)
    phone_extension: str = Field(default="", max_length=50)
    description: str = Field(default="", max_length=1000)
    status: Literal["ACTIVE", "INACTIVE", "MAINTENANCE"] = "ACTIVE"


class WardOutput(ScopedOutput):
    department_id: uuid.UUID
    ward_code: str
    name: str
    ward_type: str = "General"
    floor: str = ""
    building: str = ""
    gender_type: str = "Mixed"
    age_group: str = "All"
    bed_capacity: int = 30
    in_charge_staff_id: str = ""
    phone_extension: str = ""
    description: str = ""
    status: str = "ACTIVE"
    created_at: datetime


class BedInput(Input):
    ward_id: uuid.UUID
    room_id: uuid.UUID | None = None
    bed_number: str = Field(default="", max_length=50)
    bed_type: Literal["STANDARD", "ICU", "ISOLATION", "PEDIATRIC", "MATERNITY"] = "STANDARD"
    status: Literal["AVAILABLE", "OCCUPIED", "RESERVED", "CLEANING", "MAINTENANCE"] = "AVAILABLE"
    is_active: bool = True


class BedOutput(Output):
    id: uuid.UUID
    tenant_id: uuid.UUID
    branch_id: uuid.UUID
    ward_id: uuid.UUID
    room_id: uuid.UUID | None = None
    bed_number: str
    bed_type: str = "STANDARD"
    status: str = "AVAILABLE"
    is_active: bool = True
    created_at: datetime


class WardAdmissionInput(Input):
    patient_id: uuid.UUID
    ward_id: uuid.UUID
    bed_id: uuid.UUID | None = None
    admission_status: Literal["ADMITTED", "TRANSFERRED", "DISCHARGED", "CANCELLED"] = "ADMITTED"
    assigned_by: str = Field(default="", max_length=200)
    discharged_by: str = Field(default="", max_length=200)
    admitted_at: AwareDatetime | None = None
    planned_discharge_at: AwareDatetime | None = None


class WardAdmissionOutput(Output):
    id: uuid.UUID
    tenant_id: uuid.UUID
    branch_id: uuid.UUID
    patient_id: uuid.UUID
    ward_id: uuid.UUID
    bed_id: uuid.UUID | None = None
    admitted_at: datetime
    discharged_at: datetime | None = None
    planned_discharge_at: datetime | None = None
    bed_assigned_at: datetime | None = None
    admission_status: str = "ADMITTED"
    assigned_by: str = ""
    discharged_by: str = ""
    created_at: datetime
