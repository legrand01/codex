"""User and host-agent identity boundary for the control plane."""

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import FrozenSet, Optional
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, WebSocket

from backend.config import settings
from backend.db.pool import get_pool
from backend.dependencies import get_db

DEFAULT_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
AGENT_PATH = re.compile(
    r"^/api/v1/fleet/[0-9a-fA-F-]+/"
    r"(heartbeat|role|evidence|capabilities|commands(?:/[0-9a-fA-F-]+/result)?)$"
)
PUBLIC_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json"}


@dataclass(frozen=True)
class Principal:
    id: Optional[UUID]
    organization_id: UUID
    subject: str
    display_name: str
    role: str


DEVELOPMENT_PRINCIPAL = Principal(
    id=None,
    organization_id=DEFAULT_ORGANIZATION_ID,
    subject="development-admin",
    display_name="Development Admin",
    role="admin",
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def authenticate_user_token(token: str) -> Optional[Principal]:
    if settings.bootstrap_admin_token and hmac.compare_digest(
        token, settings.bootstrap_admin_token
    ):
        return Principal(
            id=None,
            organization_id=DEFAULT_ORGANIZATION_ID,
            subject="bootstrap-admin",
            display_name="Bootstrap Admin",
            role="admin",
        )

    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Identity store is unavailable")
    digest = hash_token(token)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE api_principals
            SET last_used_at = NOW()
            WHERE api_key_hash = $1 AND disabled = FALSE
            RETURNING id, organization_id, subject, display_name, role
            """,
            digest,
        )
    if row is None:
        return None
    return Principal(
        id=row["id"],
        organization_id=row["organization_id"],
        subject=row["subject"],
        display_name=row["display_name"],
        role=row["role"],
    )


async def authenticate_request(request: Request) -> Optional[Principal]:
    if not settings.auth_required:
        return DEVELOPMENT_PRINCIPAL
    token = _extract_bearer(request.headers.get("authorization"))
    if token is None:
        return None
    return await authenticate_user_token(token)


async def authenticate_websocket(websocket: WebSocket) -> Optional[Principal]:
    """Authenticate a WebSocket before accept using its Authorization header."""
    if not settings.auth_required:
        return DEVELOPMENT_PRINCIPAL
    token = _extract_bearer(websocket.headers.get("authorization"))
    if token is None:
        protocols = websocket.headers.get("sec-websocket-protocol", "").split(",")
        encoded = next(
            (item.strip()[7:] for item in protocols if item.strip().startswith("bearer.")),
            None,
        )
        if encoded:
            try:
                padding = "=" * (-len(encoded) % 4)
                token = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                token = None
    if token is None:
        return None
    return await authenticate_user_token(token)


def is_public_or_agent_path(request: Request) -> bool:
    if request.url.path in PUBLIC_PATHS:
        return True
    return request.method in {"GET", "POST"} and bool(
        AGENT_PATH.fullmatch(request.url.path)
    )


async def current_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication is required")
    return principal


def require_roles(*roles: str):
    allowed: FrozenSet[str] = frozenset(roles)

    async def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(status_code=403, detail="Insufficient role for this operation")
        return principal

    return dependency


async def require_agent(
    host_id: UUID,
    x_agent_token: Optional[str] = Header(default=None, alias="X-Agent-Token"),
    db=Depends(get_db),
) -> UUID:
    if not settings.agent_auth_required:
        return host_id
    if not x_agent_token:
        raise HTTPException(status_code=401, detail="Host agent token is required")
    row = await db.fetchrow(
        "SELECT agent_token_hash FROM hosts WHERE id = $1",
        host_id,
    )
    if row is None or not row["agent_token_hash"]:
        raise HTTPException(status_code=401, detail="Host agent identity is not provisioned")
    if not hmac.compare_digest(row["agent_token_hash"], hash_token(x_agent_token)):
        raise HTTPException(status_code=401, detail="Invalid host agent token")
    return host_id


async def validate_security_configuration() -> None:
    if settings.environment == "production":
        if not settings.auth_required or not settings.agent_auth_required:
            raise RuntimeError("Production requires user and host-agent authentication")
        if settings.debug or settings.demo_mode:
            raise RuntimeError("Production forbids debug and demo modes")
        if not settings.cors_origins or any(
            origin == "*" or not origin.startswith("https://")
            for origin in settings.cors_origins
        ):
            raise RuntimeError("Production CORS origins must be explicit HTTPS origins")
        if "postgres:postgres@" in settings.database_url:
            raise RuntimeError("Production cannot use the default database credentials")
        if settings.bootstrap_admin_token and len(settings.bootstrap_admin_token) < 32:
            raise RuntimeError("BOOTSTRAP_ADMIN_TOKEN must contain at least 32 characters")
        pool = get_pool()
        if pool is None:
            raise RuntimeError("Identity store is unavailable")
        async with pool.acquire() as conn:
            principal_count = await conn.fetchval(
                "SELECT COUNT(*) FROM api_principals WHERE disabled = FALSE"
            )
        if not principal_count and not settings.bootstrap_admin_token:
            raise RuntimeError(
                "Production requires an active API principal or BOOTSTRAP_ADMIN_TOKEN"
            )
