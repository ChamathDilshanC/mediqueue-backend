"""Verified Supabase identities and database-backed branch authorization."""
from dataclasses import dataclass
import uuid

import httpx
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import Membership
from .settings import get_settings

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Identity:
    subject: str
    display_name: str = ""


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    branch_id: str
    roles: tuple[str, ...] = ()


async def current_identity(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Identity:
    settings = get_settings()
    if not credentials or not settings.supabase_url:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        token = credentials.credentials
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm == "HS256" and settings.supabase_jwt_secret:
            key = settings.supabase_jwt_secret
        elif algorithm in {"RS256", "ES256"}:
            url = settings.supabase_jwks_url or f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(url)
                response.raise_for_status()
            key = next(k for k in response.json()["keys"] if k.get("kid") == header.get("kid"))
        else:
            raise ValueError("Unsupported signing algorithm")
        claims = jwt.decode(token, key, algorithms=[algorithm], audience="authenticated",
                            issuer=f"{settings.supabase_url.rstrip('/')}/auth/v1",
                            options={"require_exp": True, "require_sub": True, "require_aud": True, "require_iss": True})
        metadata = claims.get("user_metadata") or {}
        name = metadata.get("display_name", "") if isinstance(metadata, dict) else ""
        return Identity(str(uuid.UUID(claims["sub"])), name[:200] if isinstance(name, str) else "")
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Identity provider unavailable") from exc
    except (JWTError, KeyError, ValueError, TypeError, StopIteration) as exc:
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}) from exc


async def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_tenant_id: uuid.UUID | None = Header(None),
    x_branch_id: uuid.UUID | None = Header(None),
    x_dev_tenant: uuid.UUID | None = Header(None),
    x_dev_branch: uuid.UUID | None = Header(None),
    db: AsyncSession = Depends(get_session),
) -> Principal:
    settings = get_settings()
    if settings.auth_dev_header_enabled and x_dev_tenant and x_dev_branch:
        return Principal("development", str(x_dev_tenant), str(x_dev_branch), ("staff",))
    identity = await current_identity(credentials)
    query = select(Membership).where(Membership.user_id == uuid.UUID(identity.subject), Membership.active.is_(True))
    if x_tenant_id:
        query = query.where(Membership.tenant_id == x_tenant_id)
    if x_branch_id:
        query = query.where(Membership.branch_id == x_branch_id)
    memberships = (await db.scalars(query.limit(2))).all()
    if not memberships:
        raise HTTPException(403, "An active hospital membership is required")
    if len(memberships) != 1:
        raise HTTPException(400, "Select a hospital branch using X-Tenant-ID and X-Branch-ID")
    membership = memberships[0]
    return Principal(identity.subject, str(membership.tenant_id), str(membership.branch_id), (membership.role,))


def require_scope(principal: Principal, tenant_id: str, branch_id: str) -> None:
    if principal.tenant_id != tenant_id or principal.branch_id != branch_id:
        raise HTTPException(403, "Scope denied")


def require_role(principal: Principal, *roles: str) -> None:
    if not set(principal.roles).intersection(roles):
        raise HTTPException(403, "Role denied")
