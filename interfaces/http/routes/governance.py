"""Administrator-only service identity and resource authorization management."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response

from domain.governance import (
    AccessDenied,
    AccessKeyCreate,
    EnabledUpdate,
    GovernanceConflict,
    GovernanceError,
    GovernanceNotFound,
    ProjectCreate,
    ResourceGrantCreate,
    ServiceIdentityCreate,
)
from interfaces.http.api_models import (
    IssuedServiceKeyResponse,
    ProjectResponse,
    ResourceGrantResponse,
    ServiceIdentityResponse,
    ServiceKeyResponse,
)
from interfaces.http.platform_auth import AdminActor
from orchestration.governance_service import GovernanceService

router = APIRouter(prefix="/v2/projects", tags=["governance"])


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except GovernanceNotFound:
        raise HTTPException(status_code=404, detail="Governance resource not found") from None
    except AccessDenied:
        raise HTTPException(status_code=403, detail="Resource access denied") from None
    except GovernanceConflict:
        raise HTTPException(status_code=409, detail="Governance resource already exists") from None
    except GovernanceError:
        raise HTTPException(status_code=422, detail="Invalid governance request") from None


def _repository(request):
    return request.app.state.store.governance


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(request: Request, payload: ProjectCreate, actor: AdminActor):
    return _call(_repository(request).create_project, payload, actor)


@router.get("", response_model=list[ProjectResponse])
def list_projects(request: Request, _actor: AdminActor, limit: int = Query(default=100, ge=1, le=500)):
    return _repository(request).list_projects(limit=limit)


@router.get("/{project_key}", response_model=ProjectResponse)
def get_project(request: Request, project_key: str, _actor: AdminActor):
    return _call(_repository(request).get_project, project_key)


@router.patch("/{project_key}", response_model=ProjectResponse)
def set_project_enabled(request: Request, project_key: str, payload: EnabledUpdate, actor: AdminActor):
    return _call(_repository(request).set_project_enabled, project_key, payload.enabled, actor)


@router.post("/{project_key}/service-identities", status_code=201, response_model=ServiceIdentityResponse)
def create_identity(request: Request, project_key: str, payload: ServiceIdentityCreate, actor: AdminActor):
    return _call(_repository(request).create_identity, project_key, payload, actor)


@router.get("/{project_key}/service-identities", response_model=list[ServiceIdentityResponse])
def list_identities(request: Request, project_key: str, _actor: AdminActor,
                    limit: int = Query(default=100, ge=1, le=500)):
    return _call(_repository(request).list_identities, project_key, limit=limit)


@router.patch("/{project_key}/service-identities/{identity_id}", response_model=ServiceIdentityResponse)
def set_identity_enabled(request: Request, project_key: str, identity_id: str, payload: EnabledUpdate, actor: AdminActor):
    return _call(_repository(request).set_identity_enabled, project_key, identity_id, payload.enabled, actor)


@router.post("/{project_key}/service-identities/{identity_id}/keys", status_code=201, response_model=IssuedServiceKeyResponse)
def issue_key(request: Request, response: Response, project_key: str, identity_id: str,
              payload: AccessKeyCreate, actor: AdminActor):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return _call(GovernanceService(_repository(request)).issue_key, project_key, identity_id, payload, actor)


@router.get("/{project_key}/service-identities/{identity_id}/keys", response_model=list[ServiceKeyResponse])
def list_keys(request: Request, project_key: str, identity_id: str, _actor: AdminActor,
              limit: int = Query(default=100, ge=1, le=500)):
    return _call(_repository(request).list_keys, project_key, identity_id, limit=limit)


@router.post("/{project_key}/service-identities/{identity_id}/keys/{key_id}/revoke", response_model=ServiceKeyResponse)
def revoke_key(request: Request, project_key: str, identity_id: str, key_id: str, actor: AdminActor):
    return _call(_repository(request).revoke_key, project_key, identity_id, key_id, actor)


@router.post("/{project_key}/service-identities/{identity_id}/grants", status_code=201, response_model=ResourceGrantResponse)
def create_grant(request: Request, project_key: str, identity_id: str, payload: ResourceGrantCreate, actor: AdminActor):
    return _call(_repository(request).create_grant, project_key, identity_id, payload, actor)


@router.get("/{project_key}/service-identities/{identity_id}/grants", response_model=list[ResourceGrantResponse])
def list_grants(request: Request, project_key: str, identity_id: str, _actor: AdminActor,
                limit: int = Query(default=100, ge=1, le=500)):
    return _call(_repository(request).list_grants, project_key, identity_id, limit=limit)


@router.delete("/{project_key}/service-identities/{identity_id}/grants/{grant_id}", status_code=204)
def delete_grant(request: Request, project_key: str, identity_id: str, grant_id: str, actor: AdminActor):
    _call(_repository(request).delete_grant, project_key, identity_id, grant_id, actor)
