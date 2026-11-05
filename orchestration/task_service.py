"""Authenticated durable task admission and observation; never executes models or tools."""

from __future__ import annotations

import hashlib
from contextlib import nullcontext

from pydantic import TypeAdapter

from domain.capability import CapabilityConflict
from domain.schema_validation import JSONValidationError, JSONValidationUnavailable
from domain.task import (
    IdempotencyKey,
    TaskConflict,
    TaskError,
    TaskNotFound,
    TaskRecord,
    TaskSubmit,
    TaskValidationUnavailable,
)
from infra.store.repository.limits import admit_task, project_lock


class TaskService:
    def __init__(self, store, auth):
        self.store = store
        self.auth = auth

    @staticmethod
    def owner(principal):
        return f"service:{principal.identity_id}" if principal.identity_id else f"admin:{principal.name}"

    def _access(self, principal, project_key, action, capability_key=None):
        return self.auth.allowed_resources(principal, project_key, action, capability_key)

    def submit(self, principal, project_key, spec: TaskSubmit, idempotency_key, *,
               conn=None, origin_task_id=None) -> tuple[TaskRecord, bool]:
        self._access(principal, project_key, "task:submit", spec.capability_key)
        key = TypeAdapter(IdempotencyKey).validate_python(idempotency_key)
        digest = hashlib.sha256(key.encode()).hexdigest()
        fingerprint = spec.fingerprint() if origin_task_id is None else hashlib.sha256(
            f"{spec.fingerprint()}:{origin_task_id}".encode()).hexdigest()
        owner = self.owner(principal)
        repository = self.store.tasks
        with (self.store.unit_of_work() if conn is None else nullcontext(conn)) as conn:
            project_lock(conn, project_key)
            repository.lock_submission(project_key, owner, digest, conn=conn)
            existing = repository.find_submission(project_key, owner, digest, fingerprint, conn=conn)
            if existing is not None:
                return existing, False
            capability, artifact = repository.target(project_key, spec.capability_key, spec.version, conn=conn)
            if capability["kind"] not in {"agent", "workflow"}:
                raise TaskError("Only Agent and Workflow capabilities can be submitted as tasks")
            try:
                artifact.require_published(version=spec.version, capability_id=capability["id"])
            except CapabilityConflict as exc:
                raise TaskConflict(str(exc)) from None
            try:
                from infra.schema_validation import validate_json

                validate_json(spec.input, artifact.input_schema)
            except JSONValidationUnavailable as exc:
                raise TaskValidationUnavailable(exc.code) from None
            except (JSONValidationError, RecursionError):
                raise TaskError("Task input or published input schema is invalid") from None
            admit_task(conn, project_key)
            return repository.create(project_key, owner, spec, capability, artifact, digest, fingerprint,
                                     conn=conn, origin_task_id=origin_task_id), True

    def get(self, principal, project_key, task_id):
        allowed = self._access(principal, project_key, "task:read")
        return self.store.tasks.get(project_key, task_id, allowed_keys=allowed,
                                    owner=None if principal.has_scope("admin") else self.owner(principal))

    def list(self, principal, project_key, *, limit=100, business_id=None, customer_id=None):
        allowed = self._access(principal, project_key, "task:read")
        return self.store.tasks.list(project_key, allowed_keys=allowed, limit=limit,
                                     business_id=business_id, customer_id=customer_id,
                                     owner=None if principal.has_scope("admin") else self.owner(principal))

    def nodes(self, principal, project_key, task_id):
        allowed = self._access(principal, project_key, "task:read")
        return self.store.workflows.list(project_key, task_id, allowed_keys=allowed,
                                         owner=None if principal.has_scope("admin") else self.owner(principal))

    def attempts(self, principal, project_key, task_id):
        allowed = self._access(principal, project_key, "task:read")
        return self.store.tasks.attempts(project_key, task_id, allowed_keys=allowed,
                                         owner=None if principal.has_scope("admin") else self.owner(principal))

    def result(self, principal, project_key, task_id):
        allowed = self._access(principal, project_key, "task:read")
        return self.store.tasks.result(project_key, task_id, allowed_keys=allowed,
                                        owner=None if principal.has_scope("admin") else self.owner(principal))

    def model_calls(self, principal, project_key, task_id):
        allowed = self._access(principal, project_key, "task:read")
        return self.store.models.calls(project_key, task_id, allowed_keys=allowed,
                                        owner=None if principal.has_scope("admin") else self.owner(principal))

    def tool_calls(self, principal, project_key, task_id):
        allowed = self._access(principal, project_key, "task:read")
        return self.store.tool_bindings.calls(project_key, task_id, allowed_keys=allowed,
                                               owner=None if principal.has_scope("admin") else self.owner(principal))

    def deliveries(self, principal, project_key, task_id, *, limit=100, cursor=None):
        allowed = self._access(principal, project_key, "task:read")
        return self.store.callbacks.task_deliveries(
            project_key, task_id, allowed_keys=allowed,
            owner=None if principal.has_scope("admin") else self.owner(principal), limit=limit, cursor=cursor,
        )

    def cancel(self, principal, project_key, task_id):
        allowed = self._access(principal, project_key, "task:cancel")
        return self.store.task_control.request_cancel(project_key, task_id, self.owner(principal), allowed_keys=allowed,
                                                      owner=None if principal.has_scope("admin") else self.owner(principal))

    def cancellation(self, principal, project_key, task_id):
        allowed = self._access(principal, project_key, "task:read")
        return self.store.task_control.cancellation(project_key, task_id, allowed_keys=allowed,
                                                    owner=None if principal.has_scope("admin") else self.owner(principal))

    def events(self, principal, project_key, task_id, *, after_sequence=0, limit=100):
        allowed = self._access(principal, project_key, "task:read")
        return self.store.task_events.list(project_key, task_id, allowed_keys=allowed,
                                           owner=None if principal.has_scope("admin") else self.owner(principal),
                                           after_sequence=after_sequence, limit=limit)

    def recovery(self, principal, project_key, task_id):
        from framework.workflow_recovery import assess_workflow_recovery

        source = self.get(principal, project_key, task_id)
        with self.store.unit_of_work() as conn:
            _, artifact = self.store.tasks.target(project_key, source.capability_key, source.version, conn=conn)
            assessment = assess_workflow_recovery(source, artifact, artifact.content_hash)
        return assessment

    def recover(self, principal, project_key, task_id, request_key, *, conn=None) -> tuple[TaskRecord, bool]:
        from framework.workflow_recovery import assess_workflow_recovery
        from infra.store.model import AiTask

        source = self.get(principal, project_key, task_id)
        with (self.store.unit_of_work() if conn is None else nullcontext(conn)) as session:
            _, artifact = self.store.tasks.target(project_key, source.capability_key, source.version, conn=session)
            assessment = assess_workflow_recovery(source, artifact, artifact.content_hash)
            if not assessment.eligible:
                raise TaskConflict("Workflow recovery is not eligible")
            row = session.get(AiTask, task_id)
            if row is None:
                raise TaskNotFound("Recovery source Task not found")
            key = "recover:" + hashlib.sha256(f"{task_id}:{request_key}".encode()).hexdigest()
            return self.submit(principal, project_key, TaskSubmit(capability_key=source.capability_key,
                version=source.version, business_id=source.business_id, customer_id=source.customer_id,
                input=row.input), key, conn=session, origin_task_id=task_id)
