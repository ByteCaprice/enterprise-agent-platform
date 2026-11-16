"""Task admission, safe queries and durable cancellation intent/receipt."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from domain.capability import CapabilityError
from domain.gateway import GatewayExecutionRequest, GatewayExecutionResponse
from domain.governance import AccessDenied, GovernanceNotFound
from domain.platform_model import PlatformModelCallRecord
from domain.platform_tool import PlatformToolCallRecord
from domain.task import (
    BusinessReference,
    IdempotencyKey,
    TaskConflict,
    TaskError,
    TaskNotFound,
    TaskRecord,
    TaskSubmit,
    TaskValidationUnavailable,
)
from domain.task_control import TaskCancellationRecord
from domain.task_event import TaskEventPage
from domain.task_execution import TaskAttemptRecord, TaskResultRecord
from domain.workflow import WorkflowNodeRecord
from domain.workflow_recovery import WorkflowRecoveryAssessment
from infra.schema_validation import IsolatedJSONValidator
from interfaces.http.gateway_context import GatewayContext, gateway_response_headers
from interfaces.http.platform_auth import Principal
from orchestration.gateway_service import GatewayService
from orchestration.task_service import TaskService

router = APIRouter(prefix="/v2/projects/{project_key}/tasks", tags=["tasks"])


def _service(request):
    return TaskService(request.app.state.store, request.app.state.control_plane_auth, validator=IsolatedJSONValidator())


def _call(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except TaskValidationUnavailable:
        raise HTTPException(status_code=503, detail="Task validation is temporarily unavailable",
                            headers={"Retry-After": "1"}) from None
    except AccessDenied:
        raise HTTPException(status_code=403, detail="Resource access denied") from None
    except (TaskNotFound, GovernanceNotFound):
        raise HTTPException(status_code=404, detail="Task or capability version not found") from None
    except TaskConflict:
        raise HTTPException(status_code=409, detail="Task request conflicts with existing state") from None
    except (TaskError, CapabilityError):
        raise HTTPException(status_code=422, detail="Invalid task request") from None


@router.post("", status_code=202, response_model=TaskRecord,
             responses={200: {"model": TaskRecord, "description": "Existing task returned for an identical submission"}})
def submit_task(request: Request, response: Response, project_key: str, payload: TaskSubmit, principal: Principal,
                idempotency_key: Annotated[IdempotencyKey, Header()], context: GatewayContext):
    service = GatewayService(request.app.state.store, request.app.state.control_plane_auth, validator=IsolatedJSONValidator())
    admission = _call(service.submit, principal, project_key, payload, idempotency_key, context=context)
    record = admission.task
    response.status_code = 202 if admission.request.task_created else 200
    gateway_response_headers(response, context)
    response.headers["Location"] = f"/v2/projects/{project_key}/tasks/{record.id}"
    response.headers["Cache-Control"] = "no-store"
    return record


@router.post("/execute", response_model=GatewayExecutionResponse, status_code=202,
             responses={200: {"model": GatewayExecutionResponse, "description": "Task reached a terminal state"}})
async def execute_task(request: Request, response: Response, project_key: str, payload: GatewayExecutionRequest,
                       principal: Principal, idempotency_key: Annotated[IdempotencyKey, Header()], context: GatewayContext):
    """Submit once; synchronous mode only waits for the same durable Task."""
    service = _service(request)
    if payload.mode == "sync":
        await asyncio.to_thread(_call, service._access, principal, project_key, "task:read", payload.task.capability_key)
    gateway = GatewayService(request.app.state.store, request.app.state.control_plane_auth, validator=IsolatedJSONValidator())
    admission = await asyncio.to_thread(
        _call, gateway.submit, principal, project_key, payload.task, idempotency_key,
        context=context, operation="execute", delivery_mode=payload.mode,
    )
    record = admission.task
    gateway_response_headers(response, context)
    response.headers["Location"] = f"/v2/projects/{project_key}/tasks/{record.id}"
    response.headers["Cache-Control"] = "no-store"
    if payload.mode == "async":
        return GatewayExecutionResponse(task=record, request=admission.request)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + (payload.wait_seconds if payload.wait_seconds is not None else 10)
    while True:
        if record.status not in {"queued", "running"}:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return GatewayExecutionResponse(task=record, request=admission.request, wait_timed_out=True)
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(_call, service.result, principal, project_key, record.id), timeout=remaining,
                )
            except TimeoutError:
                return GatewayExecutionResponse(task=record, request=admission.request, wait_timed_out=True)
            response.status_code = 200
            return GatewayExecutionResponse(task=record, request=admission.request, result=result)
        remaining = deadline - loop.time()
        if remaining <= 0:
            return GatewayExecutionResponse(task=record, request=admission.request, wait_timed_out=True)
        await asyncio.sleep(min(0.25, remaining))
        remaining = deadline - loop.time()
        if remaining <= 0:
            return GatewayExecutionResponse(task=record, request=admission.request, wait_timed_out=True)
        try:
            record = await asyncio.wait_for(
                asyncio.to_thread(_call, service.get, principal, project_key, record.id), timeout=remaining,
            )
        except TimeoutError:
            return GatewayExecutionResponse(task=record, request=admission.request, wait_timed_out=True)


@router.get("", response_model=list[TaskRecord])
def list_tasks(request: Request, response: Response, project_key: str, principal: Principal,
               limit: int = Query(default=100, ge=1, le=500),
               business_id: BusinessReference | None = None, customer_id: BusinessReference | None = None):
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).list, principal, project_key, limit=limit,
                 business_id=business_id, customer_id=customer_id)


@router.get("/{task_id}", response_model=TaskRecord)
def get_task(request: Request, response: Response, project_key: str, task_id: str, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).get, principal, project_key, task_id)


@router.get("/{task_id}/recovery", response_model=WorkflowRecoveryAssessment)
def assess_task_recovery(request: Request, response: Response, project_key: str, task_id: str, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).recovery, principal, project_key, task_id)


@router.get("/{task_id}/nodes", response_model=list[WorkflowNodeRecord])
def list_nodes(request: Request, response: Response, project_key: str, task_id: str, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).nodes, principal, project_key, task_id)


@router.get("/{task_id}/attempts", response_model=list[TaskAttemptRecord])
def list_attempts(request: Request, response: Response, project_key: str, task_id: str, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).attempts, principal, project_key, task_id)


@router.get("/{task_id}/result", response_model=TaskResultRecord)
def get_result(request: Request, response: Response, project_key: str, task_id: str, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).result, principal, project_key, task_id)


@router.get("/{task_id}/model-calls", response_model=list[PlatformModelCallRecord])
def list_model_calls(request: Request, response: Response, project_key: str, task_id: str, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).model_calls, principal, project_key, task_id)


@router.post("/{task_id}/cancel", response_model=TaskCancellationRecord,
             responses={202: {"model": TaskCancellationRecord, "description": "Durable intent awaiting Worker acknowledgement"}})
def cancel_task(request: Request, response: Response, project_key: str, task_id: str, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    receipt = _call(_service(request).cancel, principal, project_key, task_id)
    response.status_code = 202 if receipt.task.status == "running" else 200
    return receipt


@router.get("/{task_id}/cancellation", response_model=TaskCancellationRecord)
def get_cancellation(request: Request, response: Response, project_key: str, task_id: str, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).cancellation, principal, project_key, task_id)


@router.get("/{task_id}/events", response_model=TaskEventPage)
def list_events(request: Request, response: Response, project_key: str, task_id: str, principal: Principal,
                 after_sequence: int = Query(default=0, ge=0, le=9007199254740991),
                 limit: int = Query(default=100, ge=1, le=200)):
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).events, principal, project_key, task_id, after_sequence=after_sequence, limit=limit)


@router.get("/{task_id}/tool-calls", response_model=list[PlatformToolCallRecord])
def list_tool_calls(request: Request, response: Response, project_key: str, task_id: str, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return _call(_service(request).tool_calls, principal, project_key, task_id)
