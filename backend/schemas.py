"""Strict input contracts shared by the identity and entity APIs."""
import uuid
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


class MeOutput(ProfileOutput):
    memberships: list[MembershipOutput]


class HospitalInput(Input):
    name: Name


class BranchInput(HospitalInput):
    timezone: str = Field(default="Asia/Colombo", max_length=64)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA timezone") from exc
        return value

class BranchCreateInput(BranchInput):
    tenant_id: uuid.UUID


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


class HospitalOutput(Output):
    id: uuid.UUID
    name: str


class BranchOutput(HospitalOutput):
    tenant_id: uuid.UUID
    timezone: str


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


class ScopedOutput(HospitalOutput):
    tenant_id: uuid.UUID
    branch_id: uuid.UUID


class DoctorInput(HospitalInput):
    department_id: uuid.UUID
    specialty: str = Field(default="", max_length=200)


class DoctorOutput(ScopedOutput):
    department_id: uuid.UUID
    specialty: str


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


class PatientOutput(Output):
    id: uuid.UUID
    tenant_id: uuid.UUID
    external_ref: str


class QueueInput(Input):
    name: str = Field(min_length=1, max_length=120)


class QueueOutput(ScopedOutput):
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
    status: Literal["CHECKED_IN", "CANCELLED", "COMPLETED", "NO_SHOW"]


class AppointmentOutput(VisitOutput):
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
