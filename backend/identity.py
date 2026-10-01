"""Supabase authentication facade, onboarding and membership management."""
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import Identity, Principal, bearer, current_identity, current_principal, require_role, require_scope
from .db import get_session
from .models import AuditEvent, Branch, Membership, OrganizationApplication, Queue, Tenant, UserProfile
from .schemas import (ERROR_RESPONSES, AuthResult, BranchInput, BranchOutput, Credentials, HospitalInput,
    HospitalOutput, HospitalRegister, HospitalRegistration, MeOutput, MembershipInput,
    MembershipOutput, MembershipPatch, OrganizationApplicationInput, OrganizationApplicationOutput,
    PasswordChange, ProfileInput, ProfileOutput,
    Recover, Refresh, Register)
from .settings import get_settings

router = APIRouter(prefix="/v1", responses=ERROR_RESPONSES)


async def auth_request(method: str, path: str, payload: dict | None = None, token: str | None = None):
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise HTTPException(503, "Configure SUPABASE_URL and SUPABASE_ANON_KEY to enable authentication")
    headers = {"apikey": settings.supabase_anon_key}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.request(method, f"{settings.supabase_url.rstrip('/')}/auth/v1/{path}",
                                            headers=headers, json=payload)
        if response.status_code >= 500:
            raise HTTPException(503, "Identity provider unavailable")
        if response.status_code >= 400:
            code = 429 if response.status_code == 429 else 400
            message = "Authentication request rejected. Check your credentials or confirmation status."
            if path.startswith("token?grant_type=password"):
                code, message = 401, "Invalid credentials or unconfirmed email"
            if response.status_code == 429:
                code = 429
                message = "Too many authentication requests. Try again later."
                error_headers = {}
                try:
                    provider_code = response.json().get("code")
                except (ValueError, AttributeError):
                    provider_code = None
                if provider_code in {"over_email_send_rate_limit", "over_request_rate_limit"}:
                    error_headers["X-Auth-Error-Code"] = provider_code
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    error_headers["Retry-After"] = str(min(86400, max(1, int(retry_after))))
                raise HTTPException(code, message, headers=error_headers)
            raise HTTPException(code, message)
        return response.json() if response.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Identity provider unavailable") from exc


async def ensure_profile(db: AsyncSession, identity: Identity) -> UserProfile:
    profile = await db.get(UserProfile, uuid.UUID(identity.subject))
    if profile is None:
        profile = UserProfile(id=uuid.UUID(identity.subject), display_name=identity.display_name)
        db.add(profile)
        await db.flush()
    return profile


def audit(db, p, action, entity_id):
    db.add(AuditEvent(tenant_id=uuid.UUID(p.tenant_id), actor_id=p.subject,
                      action=action, entity_id=entity_id, payload={"branch_id": p.branch_id}))


@router.post("/auth/register", tags=["Authentication"], response_model=AuthResult, status_code=201)
async def register(body: Register, response: Response):
    """Register an ordinary identity. No role/tenant metadata is accepted. Confirm email before onboarding."""
    response.headers["Cache-Control"] = "no-store"
    result = await auth_request("POST", "signup", {"email": body.email, "password": body.password,
                                                 "data": {"display_name": body.display_name}})
    if "id" in result:
        result = {"user": result}
    result["confirmation_required"] = not bool(result.get("access_token"))
    return result


@router.post("/auth/login", tags=["Authentication"], response_model=AuthResult)
async def login(body: Credentials, response: Response):
    """Exchange an email and password for a Supabase session."""
    response.headers["Cache-Control"] = "no-store"
    return await auth_request("POST", "token?grant_type=password", body.model_dump())


@router.post("/auth/refresh", tags=["Authentication"], response_model=AuthResult)
async def refresh(body: Refresh, response: Response):
    """Rotate the refresh token; store the newly returned token pair."""
    response.headers["Cache-Control"] = "no-store"
    return await auth_request("POST", "token?grant_type=refresh_token", body.model_dump())


@router.post("/auth/logout", tags=["Authentication"], status_code=204)
async def logout(identity: Identity = Depends(current_identity), credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    """Revoke refresh sessions. Issued access tokens remain valid until their expiry."""
    await auth_request("POST", "logout?scope=global", token=credentials.credentials)
    return Response(status_code=204)


@router.post("/auth/forgot-password", tags=["Authentication"], status_code=202)
async def forgot_password(body: Recover):
    """Send recovery instructions using the redirect URL configured in Supabase."""
    await auth_request("POST", "recover", body.model_dump())
    return {"message": "If the account is eligible, recovery instructions will be sent."}


@router.put("/auth/password", tags=["Authentication"], status_code=204)
async def change_password(body: PasswordChange, identity: Identity = Depends(current_identity),
                          credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    """Set a password with a verified signed-in or recovery session."""
    await auth_request("PUT", "user", body.model_dump(), token=credentials.credentials)
    return Response(status_code=204)


@router.get("/auth/me", tags=["Users"], response_model=MeOutput)
async def me(identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    profile = await ensure_profile(db, identity)
    memberships = (await db.scalars(select(Membership).where(Membership.user_id == profile.id))).all()
    await db.commit()
    return {"id": profile.id, "display_name": profile.display_name, "memberships": memberships}


@router.patch("/users/me", tags=["Users"], response_model=ProfileOutput)
async def update_me(body: ProfileInput, identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    profile = await ensure_profile(db, identity)
    profile.display_name = body.display_name
    await db.commit()
    return profile


@router.post("/hospitals", tags=["Hospitals"], response_model=HospitalRegistration, status_code=201)
async def register_hospital(body: HospitalRegister, identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session)):
    """Register a hospital, its first branch and the verified caller's admin membership atomically."""
    profile = await ensure_profile(db, identity)
    tenant = Tenant(name=body.name)
    db.add(tenant)
    await db.flush()
    branch = Branch(tenant_id=tenant.id, name=body.branch_name, timezone=body.timezone)
    db.add(branch)
    await db.flush()
    membership = Membership(user_id=profile.id, tenant_id=tenant.id, branch_id=branch.id, role="admin")
    db.add(membership)
    audit(db, Principal(identity.subject, str(tenant.id), str(branch.id), ("admin",)), "hospital.created", tenant.id)
    await db.commit()
    return {"hospital": tenant, "branch": branch, "membership": membership}

@router.post("/hospital-applications", tags=["Hospitals"], response_model=OrganizationApplicationOutput, status_code=201)
async def apply_for_organization(
    body: OrganizationApplicationInput,
    identity: Identity = Depends(current_identity),
    db: AsyncSession = Depends(get_session),
):
    """Submit a hospital or medical-center application for manual verification."""
    profile = await ensure_profile(db, identity)
    application = OrganizationApplication(
        applicant_id=profile.id,
        **body.model_dump(),
    )
    db.add(application)
    await db.commit()
    await db.refresh(application)
    return application


@router.get("/hospitals", tags=["Hospitals"], response_model=list[HospitalOutput])
async def hospitals(identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session),
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    """List only hospitals where the caller has an active membership."""
    return (await db.scalars(select(Tenant).join(Membership, Tenant.id == Membership.tenant_id)
        .where(Membership.user_id == uuid.UUID(identity.subject), Membership.active.is_(True))
        .distinct().order_by(Tenant.id).offset(offset).limit(limit))).all()


async def hospital_for(p, hospital_id, db):
    if str(hospital_id) != p.tenant_id:
        raise HTTPException(404, "Hospital not found")
    tenant = await db.get(Tenant, hospital_id)
    if tenant is None:
        raise HTTPException(404, "Hospital not found")
    return tenant


@router.get("/hospitals/{hospital_id}", tags=["Hospitals"], response_model=HospitalOutput)
async def hospital(hospital_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    return await hospital_for(p, hospital_id, db)


@router.patch("/hospitals/{hospital_id}", tags=["Hospitals"], response_model=HospitalOutput)
async def edit_hospital(hospital_id: uuid.UUID, body: HospitalInput, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    require_role(p, "admin")
    tenant = await hospital_for(p, hospital_id, db)
    tenant.name = body.name
    audit(db, p, "hospital.updated", tenant.id)
    await db.commit()
    return tenant


@router.get("/branches", tags=["Branches"], response_model=list[BranchOutput])
async def branches(identity: Identity = Depends(current_identity), db: AsyncSession = Depends(get_session),
                   limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return (await db.scalars(select(Branch).join(Membership, Branch.id == Membership.branch_id)
        .where(Membership.user_id == uuid.UUID(identity.subject), Membership.active.is_(True))
        .order_by(Branch.id).offset(offset).limit(limit))).all()


@router.post("/branches", tags=["Branches"], response_model=BranchOutput, status_code=201)
async def create_branch(body: BranchInput, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    require_role(p, "admin")
    branch = Branch(tenant_id=uuid.UUID(p.tenant_id), **body.model_dump())
    db.add(branch)
    await db.flush()
    db.add(Membership(user_id=uuid.UUID(p.subject), tenant_id=branch.tenant_id, branch_id=branch.id, role="admin"))
    audit(db, p, "branch.created", branch.id)
    await db.commit()
    return branch


@router.get("/branches/{branch_id}", tags=["Branches"], response_model=BranchOutput)
async def branch_detail(branch_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    require_scope(p, p.tenant_id, str(branch_id))
    branch = await db.get(Branch, branch_id)
    if branch is None or str(branch.tenant_id) != p.tenant_id:
        raise HTTPException(404, "Branch not found")
    return branch


@router.patch("/branches/{branch_id}", tags=["Branches"], response_model=BranchOutput)
async def edit_branch(branch_id: uuid.UUID, body: BranchInput, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    require_role(p, "admin")
    require_scope(p, p.tenant_id, str(branch_id))
    branch = await db.scalar(select(Branch).where(Branch.id == branch_id, Branch.tenant_id == uuid.UUID(p.tenant_id)).with_for_update())
    if branch is None:
        raise HTTPException(404, "Branch not found")
    branch.name, branch.timezone = body.name, body.timezone
    await db.execute(update(Queue).where(Queue.branch_id == branch_id, Queue.tenant_id == branch.tenant_id).values(timezone=body.timezone))
    audit(db, p, "branch.updated", branch.id)
    await db.commit()
    return branch


@router.get("/users", tags=["Users"], response_model=list[ProfileOutput])
async def users(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session),
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    require_role(p, "admin")
    return (await db.scalars(select(UserProfile).join(Membership).where(
        Membership.tenant_id == uuid.UUID(p.tenant_id), Membership.branch_id == uuid.UUID(p.branch_id))
        .order_by(UserProfile.id).offset(offset).limit(limit))).all()


@router.get("/memberships", tags=["Memberships"], response_model=list[MembershipOutput])
async def memberships(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session),
                      limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    require_role(p, "admin")
    return (await db.scalars(select(Membership).where(Membership.tenant_id == uuid.UUID(p.tenant_id),
        Membership.branch_id == uuid.UUID(p.branch_id)).order_by(Membership.id).offset(offset).limit(limit))).all()


@router.post("/memberships", tags=["Memberships"], response_model=MembershipOutput, status_code=201)
async def add_membership(body: MembershipInput, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    """Add an already registered user to the selected branch. The user must first call /auth/me."""
    require_role(p, "admin")
    if not await db.get(UserProfile, body.user_id):
        raise HTTPException(404, "Registered user profile not found")
    membership = Membership(user_id=body.user_id, tenant_id=uuid.UUID(p.tenant_id), branch_id=uuid.UUID(p.branch_id), role=body.role)
    db.add(membership)
    await db.flush()
    audit(db, p, "membership.created", membership.id)
    await db.commit()
    return membership


@router.patch("/memberships/{membership_id}", tags=["Memberships"], response_model=MembershipOutput)
async def edit_membership(membership_id: uuid.UUID, body: MembershipPatch, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)):
    require_role(p, "admin")
    # Serialize role changes per branch so simultaneous requests cannot remove every admin.
    await db.scalar(select(Branch).where(Branch.id == uuid.UUID(p.branch_id)).with_for_update())
    member = await db.get(Membership, membership_id)
    if not member:
        raise HTTPException(404, "Membership not found")
    require_scope(p, str(member.tenant_id), str(member.branch_id))
    if member.role == "admin" and member.active and (body.role != "admin" or not body.active):
        others = await db.scalar(select(Membership.id).where(Membership.branch_id == member.branch_id,
            Membership.role == "admin", Membership.active.is_(True), Membership.id != member.id))
        if not others:
            raise HTTPException(409, "A branch must retain at least one active admin")
    member.role, member.active = body.role, body.active
    audit(db, p, "membership.updated", member.id)
    await db.commit()
    return member
