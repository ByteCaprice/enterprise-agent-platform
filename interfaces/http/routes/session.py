"""Authenticated identity metadata, independent of any frontend implementation."""

from fastapi import APIRouter, Response

from interfaces.http.api_models import SessionMetadata
from interfaces.http.platform_auth import Principal

router = APIRouter(tags=["session"])


@router.get("/v2/session", response_model=SessionMetadata)
def session_metadata(response: Response, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return SessionMetadata(name=principal.name, is_admin=principal.has_scope("admin"),
                           project_key=principal.project_key, identity_id=principal.identity_id)
