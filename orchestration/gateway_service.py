"""Atomic admission + content-free request provenance; never owns execution state."""

from typing import Literal

from domain.gateway import (
    DeliveryMode,
    GatewayAdmission,
    GatewayOperation,
    GatewayRequestContext,
    GatewayRequestCreate,
)
from domain.schema_validation import JSONSchemaValidator
from domain.task import TaskError, TaskRecord, TaskSubmit
from domain.task_admission import AdmissionScope
from orchestration.task_service import TaskService


class GatewayService:
    def __init__(self, store, auth, *, validator: JSONSchemaValidator | None = None):
        self.store = store
        self.auth = auth
        self.tasks = TaskService(store, auth, validator=validator)

    def _record(self, task: TaskRecord, created: bool, context: GatewayRequestContext, operation: GatewayOperation,
                 delivery_mode: DeliveryMode, *, admission: AdmissionScope) -> GatewayAdmission:
        command = GatewayRequestCreate(
            id=context.request_id, correlation_id=context.correlation_id,
            project_key=task.project_key, task_id=task.id, submitted_by=task.submitted_by,
            operation=operation, delivery_mode=delivery_mode, task_created=created,
        )
        request = admission.record_gateway(command)
        return GatewayAdmission(task=task, request=request)

    def submit(self, principal, project_key, spec: TaskSubmit, idempotency_key, *, context: GatewayRequestContext,
               operation: Literal["submit", "execute"] = "submit", delivery_mode: DeliveryMode = "async") -> GatewayAdmission:
        if operation not in {"submit", "execute"} or delivery_mode not in {"sync", "async"} or (
            operation == "submit" and delivery_mode != "async"
        ):
            raise TaskError("Invalid gateway delivery preferences")
        # Task creation/reuse and request evidence commit together. A failed
        # evidence insert cannot leave an untraceable newly accepted Task.
        prepared = self.tasks.prepare(principal, project_key, spec, idempotency_key)
        with self.tasks.admission.unit_of_work(project_key, self.tasks.owner(principal)) as admission:
            task, created = admission.admit(prepared)
            return self._record(task, created, context, operation, delivery_mode, admission=admission)

    def recover(self, principal, project_key, task_id, request_key, *, context: GatewayRequestContext) -> GatewayAdmission:
        prepared = self.tasks.prepare_recovery(principal, project_key, task_id, request_key)
        with self.tasks.admission.unit_of_work(project_key, self.tasks.owner(principal)) as admission:
            task, created = admission.admit(prepared)
            return self._record(task, created, context, "recover", "async", admission=admission)

    def _read_scope(self, principal, project_key):
        allowed = self.auth.allowed_resources(principal, project_key, "task:read")
        owner = None if principal.has_scope("admin") else TaskService.owner(principal)
        return allowed, owner

    def get(self, principal, project_key, request_id):
        allowed, owner = self._read_scope(principal, project_key)
        return self.store.gateway_requests.get(project_key, request_id, allowed_keys=allowed, owner=owner)

    def list(self, principal, project_key, task_id, *, limit=100, cursor=None):
        allowed, owner = self._read_scope(principal, project_key)
        return self.store.gateway_requests.list(project_key, task_id, allowed_keys=allowed, owner=owner,
                                                limit=limit, cursor=cursor)
