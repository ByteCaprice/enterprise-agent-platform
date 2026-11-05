"""Trusted identity dependencies shared by the new platform's HTTP routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from domain.governance import AccessDenied, GovernanceNotFound
from orchestration.control_plane_auth import ControlPlanePrincipal

_platform_key = APIKeyHeader(name="x-api-key", scheme_name="PlatformKey", auto_error=False)
_bound_oidc = HTTPBearer(scheme_name="BoundOIDC", auto_error=False,
                        description="Explicitly bound OIDC JWT; not a platform-issued login token")


def authenticated_principal(request: Request, x_api_key: Annotated[str | None, Security(_platform_key)],
                             _bearer: Annotated[HTTPAuthorizationCredentials | None, Security(_bound_oidc)],
                             authorization: str | None = Header(default=None, include_in_schema=False)) -> ControlPlanePrincipal:
    # Never fall back to old registry/open-access behavior if wiring is missing.
    auth = request.app.state.control_plane_auth
    if authorization is not None:
        if x_api_key or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Use one explicit credential mechanism")
        entry = auth.authenticate_bearer(authorization.removeprefix("Bearer "))
    else:
        entry = auth.authenticate(x_api_key)
    if entry is None:
        raise HTTPException(status_code=401, detail="Platform API key required")
    limiter = request.app.state.rate_limiter
    # Bucket by identity, not token: rotation cannot multiply the allowance.
    if limiter.settings.get("enabled") and not limiter.is_allowed(f"{entry.identity_id or 'admin'}:{entry.name}"):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    return entry


Principal = Annotated[ControlPlanePrincipal, Depends(authenticated_principal)]


def platform_admin(principal: Principal) -> str:
    if not principal.has_scope("admin"):
        raise HTTPException(status_code=403, detail="Platform administrator required")
    return principal.name


AdminActor = Annotated[str, Depends(platform_admin)]


def capability_access(action, *, collection=False, create=False):
    def authorize(request: Request, principal: Principal):
        project_key = request.path_params["project_key"]
        resource_key = "*" if create else None if collection else request.path_params["key"]
        try:
            allowed = request.app.state.control_plane_auth.allowed_resources(principal, project_key, action, resource_key)
        except AccessDenied:
            raise HTTPException(status_code=403, detail="Resource access denied") from None
        except GovernanceNotFound:
            raise HTTPException(status_code=404, detail="Project not found") from None
        request.state.allowed_capability_resources = allowed
        return principal.name
    return authorize
