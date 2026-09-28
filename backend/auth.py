"""JWT and explicitly opt-in development authentication."""
from dataclasses import dataclass
from fastapi import Header, HTTPException, status
from jose import jwt, JWTError
import httpx
from .settings import get_settings

@dataclass(frozen=True)
class Principal:
    """Authenticated actor and tenant/branch scope."""
    subject: str; tenant_id: str; branch_id: str; roles: tuple[str, ...] = ()

async def current_principal(authorization: str | None = Header(None), x_dev_tenant: str | None = Header(None), x_dev_branch: str | None = Header(None)) -> Principal:
    """Verify Supabase JWT or use development headers only when enabled."""
    s = get_settings()
    if s.auth_dev_header_enabled and x_dev_tenant and x_dev_branch:
        return Principal("development", x_dev_tenant, x_dev_branch, ("staff",))
    if not authorization or not authorization.startswith("Bearer ") or (not s.supabase_jwt_secret and not s.supabase_jwks_url):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    try:
        token = authorization[7:]
        if s.supabase_jwt_secret:
            claims = jwt.decode(token, s.supabase_jwt_secret, algorithms=["HS256"])
        else:
            header = jwt.get_unverified_header(token)
            async with httpx.AsyncClient(timeout=5) as client:
                keys = (await client.get(s.supabase_jwks_url)).json()["keys"]
            key = next(item for item in keys if item["kid"] == header["kid"])
            claims = jwt.decode(token, key, algorithms=[header.get("alg", "RS256")], audience="authenticated")
        return Principal(str(claims["sub"]), str(claims["tenant_id"]), str(claims["branch_id"]), tuple(claims.get("roles", [])))
    except (JWTError, KeyError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc

def require_scope(principal: Principal, tenant_id: str, branch_id: str) -> None:
    """Reject cross-tenant or cross-branch access."""
    if principal.tenant_id != tenant_id or principal.branch_id != branch_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Scope denied")
