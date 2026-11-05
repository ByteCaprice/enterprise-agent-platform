"""Authenticated platform operations and resource governance endpoints."""

from fastapi import APIRouter, HTTPException, Query, Request, Response
from jsonschema import SchemaError
from pydantic import TypeAdapter
from sqlalchemy.exc import IntegrityError

from domain.capability import CapabilityValidationUnavailable, Key
from domain.governance import AccessDenied, EnabledUpdate, GovernanceNotFound
from domain.platform_operations import (
    BudgetCreate,
    CallbackCreate,
    ChargeReconcile,
    ConcurrencyLimit,
    DatasetCreate,
    EvaluationCreate,
    FeedbackCreate,
    MemoryWrite,
    OIDCBindingCreate,
    OIDCProviderCreate,
    ProjectLimits,
    RecoveryCreate,
    ReviewCreate,
)
from domain.task import TaskConflict, TaskError, TaskNotFound, TaskRecord, TaskValidationUnavailable
from interfaces.http.gateway_context import GatewayContext, gateway_response_headers
from interfaces.http.platform_auth import AdminActor, Principal
from orchestration.feedback_service import FeedbackService
from orchestration.gateway_service import GatewayService
from orchestration.operations_queries import Collection, OperationsQueries
from orchestration.quality_service import QualityService
from orchestration.task_service import TaskService

router = APIRouter(prefix="/v2", tags=["operations-v1"])


@router.get("/projects/{project}/operations/pages/{collection}")
def operations_page(request: Request, project: str, collection: Collection, principal: Principal,
                    limit: int = Query(default=50, ge=1, le=200), cursor: str | None = Query(default=None, max_length=4096),
                    capability: str | None = Query(default=None, max_length=100),
                    subject: str | None = Query(default=None, max_length=100),
                    task_id: str | None = Query(default=None, max_length=200)):
    return call(OperationsQueries(request.app.state.store, request.app.state.control_plane_auth).page,
                principal, project, collection, limit=limit, cursor=cursor, capability=capability, subject=subject, task_id=task_id)


@router.get("/projects/{project}/operations/health")
def operational_health(request: Request, project: str, actor: AdminActor):
    settings = request.app.state.platform_runtime
    return call(request.app.state.store.worker_health.summary, project,
                stale_seconds=settings.worker_stale_seconds, queue_warning_seconds=settings.queue_warning_seconds)


def call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except (CapabilityValidationUnavailable, TaskValidationUnavailable):
        raise HTTPException(503, "Validation is temporarily unavailable", headers={"Retry-After": "1"}) from None
    except AccessDenied:
        raise HTTPException(403, "Resource access denied") from None
    except (TaskNotFound, GovernanceNotFound):
        raise HTTPException(404, "Operations resource not found") from None
    except (TaskConflict, IntegrityError):
        raise HTTPException(409, "Operations request conflicts with existing state") from None
    except (TaskError, ValueError, SchemaError):
        raise HTTPException(422, "Invalid operations configuration") from None


def quality(request):
    return QualityService(request.app.state.store, request.app.state.control_plane_auth)


@router.get("/projects/{project}/tasks/{task_id}/feedback")
def list_feedback(request: Request, response: Response, project: str, task_id: str, principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return call(FeedbackService(request.app.state.store, request.app.state.control_plane_auth).list,
                principal, project, task_id)


@router.post("/projects/{project}/tasks/{task_id}/feedback", status_code=201)
def add_feedback(request: Request, response: Response, project: str, task_id: str, spec: FeedbackCreate, principal: Principal):
    row, created = call(FeedbackService(request.app.state.store, request.app.state.control_plane_auth).add,
                        principal, project, task_id, spec)
    response.status_code = 201 if created else 200
    response.headers["Cache-Control"] = "no-store"
    return row


@router.get("/operations/concurrency")
def get_global_concurrency(request: Request, actor: AdminActor):
    return call(request.app.state.store.operations.global_concurrency)


@router.put("/operations/concurrency")
def put_global_concurrency(request: Request, spec: ConcurrencyLimit, actor: AdminActor):
    return call(request.app.state.store.operations.global_concurrency, spec, actor)


@router.get("/projects/{project}/capabilities/{capability_key}/concurrency")
def get_capability_concurrency(request: Request, project: str, capability_key: str, actor: AdminActor):
    return call(request.app.state.store.operations.capability_concurrency, project, capability_key)


@router.put("/projects/{project}/capabilities/{capability_key}/concurrency")
def put_capability_concurrency(request: Request, project: str, capability_key: str, spec: ConcurrencyLimit,
                               actor: AdminActor):
    return call(request.app.state.store.operations.capability_concurrency, project, capability_key, spec, actor)


@router.get("/projects/{project}/operations/limits")
def get_limits(request: Request, project: str, actor: AdminActor):
    return call(request.app.state.store.operations.limits, project)


@router.put("/projects/{project}/operations/limits")
def put_limits(request: Request, project: str, spec: ProjectLimits, actor: AdminActor):
    return call(request.app.state.store.operations.limits, project, spec, actor)


@router.get("/projects/{project}/operations/budgets")
def budgets(request: Request, project: str, actor: AdminActor):
    return call(request.app.state.store.operations.budgets, project)


@router.post("/projects/{project}/operations/budgets", status_code=201)
def create_budget(request: Request, project: str, spec: BudgetCreate, actor: AdminActor):
    return call(request.app.state.store.operations.create_budget, project, spec, actor)


@router.get("/projects/{project}/operations/charges")
def charges(request: Request, project: str, actor: AdminActor):
    return call(request.app.state.store.operations.charges, project)


@router.post("/projects/{project}/operations/charges/{call_id}/reconcile")
def reconcile(request: Request, project: str, call_id: str, spec: ChargeReconcile, actor: AdminActor):
    return call(request.app.state.store.operations.reconcile, project, call_id, spec, actor)


@router.get("/projects/{project}/operations/callbacks")
def callbacks(request: Request, project: str, actor: AdminActor):
    return call(request.app.state.store.callbacks.endpoints, project)


@router.post("/projects/{project}/operations/callbacks", status_code=201)
def create_callback(request: Request, project: str, spec: CallbackCreate, actor: AdminActor):
    return call(request.app.state.store.callbacks.endpoints, project, spec, actor)


@router.patch("/projects/{project}/operations/callbacks/{key}")
def enable_callback(request: Request, project: str, key: str, spec: EnabledUpdate, actor: AdminActor):
    return call(request.app.state.store.callbacks.enabled, project, key, spec.enabled, actor)


@router.get("/projects/{project}/operations/deliveries")
def deliveries(request: Request, project: str, actor: AdminActor):
    return call(request.app.state.store.callbacks.deliveries, project)


@router.get("/projects/{project}/quality/datasets")
def datasets(request: Request, project: str, actor: AdminActor, principal: Principal):
    return call(quality(request).datasets, principal, project)


@router.post("/projects/{project}/quality/datasets", status_code=201)
def create_dataset(request: Request, project: str, spec: DatasetCreate, actor: AdminActor, principal: Principal):
    return call(quality(request).datasets, principal, project, spec)


@router.post("/projects/{project}/quality/runs", status_code=202)
def create_evaluation(request: Request, project: str, spec: EvaluationCreate, principal: Principal):
    return call(quality(request).create, principal, project, spec)


@router.get("/projects/{project}/quality/runs")
def evaluations(request: Request, project: str, principal: Principal):
    return call(quality(request).runs, principal, project)


@router.post("/projects/{project}/quality/runs/{run_id}/score")
def score(request: Request, project: str, run_id: str, principal: Principal):
    return call(quality(request).score, principal, project, run_id)


@router.get("/projects/{project}/quality/runs/{left_id}/compare/{right_id}")
def compare_evaluations(request: Request, response: Response, project: str, left_id: str, right_id: str,
                        principal: Principal):
    response.headers["Cache-Control"] = "no-store"
    return call(quality(request).compare, principal, project, left_id, right_id)


@router.get("/projects/{project}/tasks/{task_id}/reviews")
def reviews(request: Request, project: str, task_id: str, principal: Principal):
    return call(quality(request).reviews, principal, project, task_id)


@router.post("/projects/{project}/tasks/{task_id}/reviews", status_code=201)
def add_review(request: Request, project: str, task_id: str, spec: ReviewCreate, principal: Principal):
    return call(quality(request).reviews, principal, project, task_id, spec)


@router.post("/projects/{project}/tasks/{task_id}/recover", status_code=202, response_model=TaskRecord,
             responses={200: {"model": TaskRecord, "description": "Existing recovery Task for an identical request key"}})
def recover(request: Request, response: Response, project: str, task_id: str, spec: RecoveryCreate,
            principal: Principal, context: GatewayContext):
    service = GatewayService(request.app.state.store, request.app.state.control_plane_auth)
    admission = call(service.recover, principal, project, task_id, spec.request_key, context=context)
    response.status_code = 202 if admission.request.task_created else 200
    response.headers["Location"] = f"/v2/projects/{project}/tasks/{admission.task.id}"
    response.headers["Cache-Control"] = "no-store"
    gateway_response_headers(response, context)
    return admission.task


@router.get("/projects/{project}/memory/{capability}/{subject}")
def memory(request: Request, project: str, capability: str, subject: str, principal: Principal):
    def operation():
        TypeAdapter(Key).validate_python(capability)
        TypeAdapter(Key).validate_python(subject)
        request.app.state.control_plane_auth.allowed_resources(principal, project, "memory:read", capability)
        return request.app.state.store.memory.read(project, TaskService.owner(principal), capability, subject)
    return call(operation)


@router.post("/projects/{project}/memory", status_code=201)
def write_memory(request: Request, project: str, spec: MemoryWrite, principal: Principal):
    def operation():
        request.app.state.control_plane_auth.allowed_resources(principal, project, "memory:write", spec.capability_key)
        return request.app.state.store.memory.write(project, TaskService.owner(principal), spec)
    return call(operation)


@router.get("/oidc/providers")
def oidc_providers(request: Request, actor: AdminActor):
    return call(request.app.state.store.oidc.providers)


@router.put("/oidc/providers")
def install_oidc(request: Request, spec: OIDCProviderCreate, actor: AdminActor):
    return call(request.app.state.store.oidc.providers, spec, actor)


@router.patch("/oidc/providers/{key}")
def enable_oidc(request: Request, key: str, spec: EnabledUpdate, actor: AdminActor):
    return call(request.app.state.store.oidc.enabled, key, spec.enabled, actor)


@router.get("/projects/{project}/oidc/bindings")
def bindings(request: Request, project: str, actor: AdminActor):
    return call(request.app.state.store.oidc.bindings, project)


@router.post("/projects/{project}/oidc/bindings", status_code=201)
def bind_oidc(request: Request, project: str, spec: OIDCBindingCreate, actor: AdminActor):
    return call(request.app.state.store.oidc.bindings, project, spec, actor)


@router.post("/projects/{project}/oidc/unbind")
def unbind_oidc(request: Request, project: str, spec: OIDCBindingCreate, actor: AdminActor):
    return call(request.app.state.store.oidc.unbind, project, spec.provider_key, spec.subject, actor)
