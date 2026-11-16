"""Platform control-plane capability and Prompt Bank APIs. Always fail closed."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from domain.capability import (
    CapabilityConflict,
    CapabilityError,
    CapabilityNotFound,
    CapabilityRecord,
    CapabilitySpec,
    CapabilityValidationUnavailable,
    CapabilityVersionDraft,
    CapabilityVersionRecord,
    PromptRenderRequest,
)
from domain.code_workflow_plan import CodeWorkflowPlanRecord
from domain.workflow import WorkflowPlanRecord
from infra.schema_validation import IsolatedJSONValidator
from interfaces.http.api_models import PromptRenderResponse
from interfaces.http.platform_auth import capability_access
from orchestration.capability_service import CapabilityService
from orchestration.prompt_renderer import PromptRenderer
from orchestration.workflow_plan_query import WorkflowPlanQuery

router = APIRouter(prefix="/v2/projects/{project_key}", tags=["capabilities"])


CreateActor = Annotated[str, Depends(capability_access("capability:write", create=True))]
ReadActor = Annotated[str, Depends(capability_access("capability:read"))]
ListActor = Annotated[str, Depends(capability_access("capability:read", collection=True))]
WriteActor = Annotated[str, Depends(capability_access("capability:write"))]
PublishActor = Annotated[str, Depends(capability_access("capability:publish"))]
RenderActor = Annotated[str, Depends(capability_access("prompt:render"))]


def _service(request):
    return CapabilityService(request.app.state.store.capabilities, IsolatedJSONValidator())


def _call(operation, *args):
    try:
        return operation(*args)
    except CapabilityValidationUnavailable:
        raise HTTPException(status_code=503, detail="Capability validation is temporarily unavailable",
                            headers={"Retry-After": "1"}) from None
    except CapabilityNotFound:
        raise HTTPException(status_code=404, detail="Capability or version not found") from None
    except CapabilityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except CapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid capability definition") from None


@router.post("/capabilities", status_code=201, response_model=CapabilityRecord)
def create_capability(request: Request, project_key: str, payload: CapabilitySpec, actor: CreateActor):
    return _call(_service(request).create, project_key, payload, actor)


@router.get("/capabilities", response_model=list[CapabilityRecord])
def list_capabilities(request: Request, project_key: str, _actor: ListActor,
                      limit: int = Query(default=100, ge=1, le=500)):
    return request.app.state.store.capabilities.list(
        project_key, limit=limit, allowed_keys=request.state.allowed_capability_resources,
    )


@router.get("/capabilities/{key}", response_model=CapabilityRecord)
def get_capability(request: Request, project_key: str, key: str, _actor: ReadActor):
    return _call(request.app.state.store.capabilities.get, project_key, key)


@router.post("/capabilities/{key}/versions", status_code=201, response_model=CapabilityVersionRecord)
def create_draft(request: Request, project_key: str, key: str, payload: CapabilityVersionDraft,
                  actor: WriteActor):
    return _call(_service(request).create_draft, project_key, key, payload, actor)


@router.get("/capabilities/{key}/versions", response_model=list[CapabilityVersionRecord])
def list_versions(request: Request, project_key: str, key: str, _actor: ReadActor,
                  limit: int = Query(default=100, ge=1, le=500)):
    try:
        return request.app.state.store.capabilities.list_versions(project_key, key, limit=limit)
    except CapabilityNotFound:
        raise HTTPException(status_code=404, detail="Capability not found") from None


@router.get("/capabilities/{key}/versions/{version}", response_model=CapabilityVersionRecord)
def get_version(request: Request, project_key: str, key: str, version: str, _actor: ReadActor):
    return _call(request.app.state.store.capabilities.get_version, project_key, key, version)


@router.get("/capabilities/{key}/versions/{version}/plan", response_model=WorkflowPlanRecord | CodeWorkflowPlanRecord)
def get_workflow_plan(request: Request, project_key: str, key: str, version: str, _actor: ReadActor):
    return _call(WorkflowPlanQuery(request.app.state.store.capabilities).describe, project_key, key, version)


@router.post("/capabilities/{key}/versions/{version}/publish", response_model=CapabilityVersionRecord)
def publish(request: Request, project_key: str, key: str, version: str, actor: PublishActor):
    return _call(_service(request).publish, project_key, key, version, actor)


@router.post("/capabilities/{key}/versions/{version}/set-default", response_model=CapabilityRecord)
def set_default(request: Request, project_key: str, key: str, version: str, actor: PublishActor):
    return _call(request.app.state.store.capabilities.set_default, project_key, key, version, actor)


@router.post("/prompts/{key}/versions/{version}/render", response_model=PromptRenderResponse)
def render_prompt(request: Request, project_key: str, key: str, version: str,
                   payload: PromptRenderRequest, _actor: RenderActor):
    return _call(PromptRenderer(request.app.state.store.capabilities).render, project_key, key, version, payload.variables)
