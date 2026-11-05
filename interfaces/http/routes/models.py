"""Administrator-only project Provider/catalog configuration; no secret values."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response

from domain.governance import EnabledUpdate, GovernanceNotFound
from domain.platform_model import (
    ModelConfigurationError,
    ModelProviderConflict,
    ModelProviderCreate,
    ModelProviderNotFound,
    ModelProviderRecord,
)
from interfaces.http.platform_auth import AdminActor

router = APIRouter(prefix="/v2/projects/{project_key}/model-providers", tags=["models"])


def _call(request, response, project_key, operation, *args, **kwargs):
    response.headers["Cache-Control"] = "no-store"
    try:
        request.app.state.store.governance.get_project(project_key)
        return operation(project_key, *args, **kwargs)
    except (GovernanceNotFound, ModelProviderNotFound):
        raise HTTPException(status_code=404, detail="Model Provider or project not found") from None
    except ModelProviderConflict:
        raise HTTPException(status_code=409, detail="Model Provider already exists") from None
    except ModelConfigurationError:
        raise HTTPException(status_code=422, detail="Invalid model configuration") from None


@router.post("", status_code=201, response_model=ModelProviderRecord)
def create_provider(request: Request, response: Response, project_key: str,
                    payload: ModelProviderCreate, actor: AdminActor):
    return _call(request, response, project_key, request.app.state.store.models.create_provider, payload, actor)


@router.get("", response_model=list[ModelProviderRecord])
def list_providers(request: Request, response: Response, project_key: str, _actor: AdminActor,
                   limit: int = Query(default=100, ge=1, le=500)):
    return _call(request, response, project_key, request.app.state.store.models.list_providers, limit=limit)


@router.get("/{provider_key}", response_model=ModelProviderRecord)
def get_provider(request: Request, response: Response, project_key: str, provider_key: str, _actor: AdminActor):
    return _call(request, response, project_key, request.app.state.store.models.provider, provider_key)


@router.patch("/{provider_key}", response_model=ModelProviderRecord)
def set_provider_enabled(request: Request, response: Response, project_key: str, provider_key: str,
                         payload: EnabledUpdate, actor: AdminActor):
    return _call(request, response, project_key, request.app.state.store.models.set_provider_enabled,
                 provider_key, payload.enabled, actor)
