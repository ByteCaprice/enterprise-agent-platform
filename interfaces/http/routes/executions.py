"""Content-free execution queries; never runs, resumes or cancels an Agent."""

from collections.abc import Callable
from typing import Annotated, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request, Response

from domain.execution_query import AgentExecutionView, AgentStepView, ExecutionTreePage, MetadataPage
from domain.governance import AccessDenied, GovernanceNotFound
from domain.task import TaskError, TaskNotFound
from interfaces.http.platform_auth import Principal
from orchestration.execution_query_service import ExecutionQueryService

router = APIRouter(prefix="/v2/projects/{project_key}/tasks/{task_id}", tags=["executions"])

PageLimit = Annotated[int, Query(ge=1, le=200)]
PageCursor = Annotated[str | None, Query(min_length=1, max_length=2048)]
AttemptFilter = Annotated[str | None, Query(min_length=1, max_length=200)]
T = TypeVar("T")


def _service(request: Request) -> ExecutionQueryService:
    return ExecutionQueryService(request.app.state.store, request.app.state.control_plane_auth)


def _query(response: Response, operation: Callable[..., T], *args, **kwargs) -> T:
    response.headers["Cache-Control"] = "no-store"
    try:
        return operation(*args, **kwargs)
    except AccessDenied:
        raise HTTPException(status_code=403, detail="Resource access denied") from None
    except (TaskNotFound, GovernanceNotFound):
        raise HTTPException(status_code=404, detail="Execution not found") from None
    except TaskError:
        raise HTTPException(status_code=422, detail="Invalid execution query") from None


@router.get("/agent-executions", response_model=MetadataPage[AgentExecutionView])
def list_agent_executions(request: Request, response: Response, project_key: str, task_id: str, principal: Principal,
                          attempt_id: AttemptFilter = None, limit: PageLimit = 100, cursor: PageCursor = None):
    return _query(response, _service(request).agent_executions, principal, project_key, task_id,
                  attempt_id=attempt_id, limit=limit, cursor=cursor)


@router.get("/agent-executions/{execution_id}", response_model=AgentExecutionView)
def get_agent_execution(request: Request, response: Response, project_key: str, task_id: str,
                        execution_id: str, principal: Principal):
    return _query(response, _service(request).agent_execution, principal, project_key, task_id, execution_id)


@router.get("/agent-executions/{execution_id}/steps", response_model=MetadataPage[AgentStepView])
def list_agent_steps(request: Request, response: Response, project_key: str, task_id: str,
                     execution_id: str, principal: Principal, limit: PageLimit = 100, cursor: PageCursor = None):
    return _query(response, _service(request).agent_steps, principal, project_key, task_id, execution_id,
                  limit=limit, cursor=cursor)


@router.get("/agent-executions/{execution_id}/steps/{step_id}", response_model=AgentStepView)
def get_agent_step(request: Request, response: Response, project_key: str, task_id: str,
                   execution_id: str, step_id: str, principal: Principal):
    return _query(response, _service(request).agent_step, principal, project_key, task_id, execution_id, step_id)


@router.get("/attempts/{attempt_id}/execution-tree", response_model=ExecutionTreePage)
def get_execution_tree(request: Request, response: Response, project_key: str, task_id: str,
                        attempt_id: str, principal: Principal, limit: PageLimit = 100, cursor: PageCursor = None):
    return _query(response, _service(request).tree, principal, project_key, task_id, attempt_id, limit=limit, cursor=cursor)
