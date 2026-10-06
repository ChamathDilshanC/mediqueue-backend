"""Verified Supabase identities and database-backed branch authorization."""
import asyncio
from dataclasses import dataclass
import time
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
JWKS_UNKNOWN_KID_REFRESH_SECONDS = 30
_jwks_cache: dict = {"url": None, "keys": [], "fetched_at": float("-inf")}
_jwks_lock = asyncio.Lock()


def _find_key(keys: list[dict], kid: str | None) -> dict | None:
    return next((key for key in keys if key.get("kid") == kid), None)


async def signing_key(url: str, kid: str | None, ttl: int) -> dict:
    """Return a JWKS key, refreshing on expiry or on an unseen key id (rotation)."""
    cache = _jwks_cache
    if cache["url"] == url and time.monotonic() - cache["fetched_at"] < ttl:
        key = _find_key(cache["keys"], kid)
        if key is not None:
            return key
    async with _jwks_lock:
        now = time.monotonic()
        age = now - cache["fetched_at"] if cache["url"] == url else float("inf")
        key = _find_key(cache["keys"], kid) if cache["url"] == url else None
        if age >= ttl or (key is None and age >= JWKS_UNKNOWN_KID_REFRESH_SECONDS):
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                keys = response.json()["keys"]
                cache.update(url=url, keys=keys, fetched_at=now)
                key = _find_key(keys, kid)
            except httpx.HTTPError:
                # Keep verifying with the last good key set during a provider hiccup.
                if key is None:
                    raise
        if key is None:
            raise KeyError("Unknown signing key")
        return key


@dataclass(frozen=True)
class Identity:
    subject: str
    display_name: str = ""
    email: str = ""


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
            key = await signing_key(url, header.get("kid"), settings.jwks_cache_seconds)
        else:
            raise ValueError("Unsupported signing algorithm")
        claims = jwt.decode(token, key, algorithms=[algorithm], audience="authenticated",
                            issuer=f"{settings.supabase_url.rstrip('/')}/auth/v1",
                            options={"require_exp": True, "require_sub": True, "require_aud": True, "require_iss": True})
        metadata = claims.get("user_metadata") or {}
        name = metadata.get("display_name", "") if isinstance(metadata, dict) else ""
        email = claims.get("email", "")
        return Identity(str(uuid.UUID(claims["sub"])), name[:200] if isinstance(name, str) else "", email)
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


async def fetch_auth_user(access_token: str) -> dict:
    """Read the authoritative Supabase user record (confirmation state is not in user-editable claims)."""
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise HTTPException(503, "Identity provider is not configured")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{settings.supabase_url.rstrip('/')}/auth/v1/user",
                                        headers={"apikey": settings.supabase_anon_key,
                                                 "Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Identity provider unavailable") from exc
    if response.status_code in (401, 403):
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    if response.status_code >= 400:
        raise HTTPException(503, "Identity provider unavailable")
    return response.json()


async def current_system_admin(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Identity:
    """Platform operators: allowlisted (SYSTEM_ADMIN_EMAILS) and with a confirmed email."""
    identity = await current_identity(credentials)
    email = identity.email.strip().lower()
    if not email or email not in get_settings().system_admins:
        raise HTTPException(403, "System admin role required")
    user = await fetch_auth_user(credentials.credentials)
    if (str(user.get("id")) != identity.subject or str(user.get("email") or "").strip().lower() != email
            or not user.get("email_confirmed_at")):
        raise HTTPException(403, "System admin role required")
    return identity
