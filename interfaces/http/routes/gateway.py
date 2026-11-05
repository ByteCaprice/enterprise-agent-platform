"""Observe accepted requests and independent delivery under live Task read scope."""

from fastapi import APIRouter, HTTPException, Query, Request, Response

from domain.callback_delivery import CallbackDeliveryPage
from domain.gateway import GatewayRequestPage, GatewayRequestRecord, RequestId
from domain.governance import AccessDenied, GovernanceNotFound
from domain.task import TaskError, TaskNotFound
from interfaces.http.platform_auth import Principal
from orchestration.gateway_service import GatewayService
from orchestration.task_service import TaskService

router = APIRouter(prefix="/v2/projects/{project_key}", tags=["task-observations"])


def _service(request):
    return GatewayService(request.app.state.store, request.app.state.control_plane_auth)


def _query(response, callback, *args, **kwargs):
    response.headers["Cache-Control"] = "no-store"
    try:
        return callback(*args, **kwargs)
    except AccessDenied:
        raise HTTPException(403, "Resource access denied") from None
    except (TaskNotFound, GovernanceNotFound):
        raise HTTPException(404, "Task or observation not found") from None
    except TaskError:
        raise HTTPException(422, "Invalid task observation query") from None


@router.get("/requests/{request_id}", response_model=GatewayRequestRecord)
def get_gateway_request(request: Request, response: Response, project_key: str,
                        request_id: RequestId, principal: Principal):
    return _query(response, _service(request).get, principal, project_key, request_id)


@router.get("/tasks/{task_id}/requests", response_model=GatewayRequestPage)
def list_gateway_requests(request: Request, response: Response, project_key: str, task_id: str, principal: Principal,
                          limit: int = Query(default=100, ge=1, le=200),
                          cursor: str | None = Query(default=None, min_length=1, max_length=2048)):
    return _query(response, _service(request).list, principal, project_key, task_id, limit=limit, cursor=cursor)


@router.get("/tasks/{task_id}/deliveries", response_model=CallbackDeliveryPage)
def list_task_deliveries(request: Request, response: Response, project_key: str, task_id: str, principal: Principal,
                         limit: int = Query(default=100, ge=1, le=200),
                         cursor: str | None = Query(default=None, min_length=1, max_length=2048)):
    service = TaskService(request.app.state.store, request.app.state.control_plane_auth)
    return _query(response, service.deliveries, principal, project_key, task_id, limit=limit, cursor=cursor)
