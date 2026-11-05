"""Project administrator APIs for immutable, explicitly read-only tool bindings."""

from fastapi import APIRouter, HTTPException, Query, Request, Response

from domain.governance import EnabledUpdate, GovernanceNotFound
from domain.platform_tool import (
    ToolBindingCreate,
    ToolBindingNotFound,
    ToolBindingRecord,
    ToolConfigurationError,
    ToolConflict,
)
from interfaces.http.platform_auth import AdminActor

router = APIRouter(prefix="/v2/projects/{project_key}/tool-bindings", tags=["tools"])


def _call(request, response, project_key, operation, *args, **kwargs):
    response.headers["Cache-Control"] = "no-store"
    try:
        request.app.state.store.governance.get_project(project_key)
        return operation(project_key, *args, **kwargs)
    except (GovernanceNotFound, ToolBindingNotFound):
        raise HTTPException(status_code=404, detail="Project or tool binding not found") from None
    except ToolConflict:
        raise HTTPException(status_code=409, detail="Tool binding already exists") from None
    except ToolConfigurationError:
        raise HTTPException(status_code=422, detail="Invalid tool configuration") from None


@router.post("", status_code=201, response_model=ToolBindingRecord)
def create_binding(request: Request, response: Response, project_key: str, payload: ToolBindingCreate, actor: AdminActor):
    return _call(request, response, project_key, request.app.state.store.tool_bindings.create_binding, payload, actor)


@router.get("", response_model=list[ToolBindingRecord])
def list_bindings(request: Request, response: Response, project_key: str, _actor: AdminActor,
                  limit: int = Query(default=100, ge=1, le=500)):
    return _call(request, response, project_key, request.app.state.store.tool_bindings.list_bindings, limit=limit)


@router.get("/{binding_key}", response_model=ToolBindingRecord)
def get_binding(request: Request, response: Response, project_key: str, binding_key: str, _actor: AdminActor):
    return _call(request, response, project_key, request.app.state.store.tool_bindings.binding, binding_key)


@router.patch("/{binding_key}", response_model=ToolBindingRecord)
def set_binding_enabled(request: Request, response: Response, project_key: str, binding_key: str,
                        payload: EnabledUpdate, actor: AdminActor):
    return _call(request, response, project_key, request.app.state.store.tool_bindings.set_binding_enabled,
                 binding_key, payload.enabled, actor)
