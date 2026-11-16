"""Authenticated durable task admission and observation; never executes models or tools."""

from __future__ import annotations

import hashlib

from pydantic import TypeAdapter

from domain.capability import CapabilityConflict
from domain.schema_validation import JSONSchemaValidator, JSONValidationError, JSONValidationUnavailable
from domain.task import (
    IdempotencyKey,
    TaskConflict,
    TaskError,
    TaskRecord,
    TaskSubmit,
    TaskValidationUnavailable,
)
from domain.task_admission import PreparedTask, Submission, TaskAdmission


class TaskService:
    def __init__(self, store, auth, *, validator: JSONSchemaValidator | None = None):
        self.store = store
        self.auth = auth
        self.validator = validator

    @property
    def admission(self) -> TaskAdmission:
        return self.store.admission

    @staticmethod
    def owner(principal):
        return f"service:{principal.identity_id}" if principal.identity_id else f"admin:{principal.name}"

    def _access(self, principal, project_key, action, capability_key=None):
        return self.auth.allowed_resources(principal, project_key, action, capability_key)

    def prepare(self, principal, project_key, spec: TaskSubmit, idempotency_key, *, recovery=None) -> PreparedTask:
        """Authorize and freeze input, then validate without a write transaction.

        A failure is deferred until the final replay check, not a reservation or
        permission to create work. No target/validator is needed for a replay.
        """
        key = TypeAdapter(IdempotencyKey).validate_python(idempotency_key)
        request = Submission.snapshot(project_key, self.owner(principal), spec, key,
                                      recovery.task().id if recovery is not None else None)
        spec = request.spec()
        self._access(principal, project_key, "task:submit", spec.capability_key)
        if self.admission.find_submission(request) is not None:
            return PreparedTask(request)
        try:
            target = self.admission.target(project_key, spec.capability_key, spec.version)
            artifact = target.artifact()
            if target.kind not in {"agent", "workflow"}:
                raise TaskError("Only Agent and Workflow capabilities can be submitted as tasks")
            try:
                artifact.require_published(version=spec.version, capability_id=target.capability_id,
                                           content_hash=recovery.task().content_hash if recovery is not None else None)
            except CapabilityConflict as exc:
                raise TaskConflict(str(exc)) from None
            if recovery is not None:
                from framework.workflow_recovery import assess_workflow_recovery

                if target.capability_id != recovery.capability_id or not assess_workflow_recovery(
                    recovery.task(), artifact, artifact.content_hash
                ).eligible:
                    raise TaskConflict("Workflow recovery is not eligible")
            try:
                if self.validator is None:
                    raise JSONValidationUnavailable("VALIDATOR_UNAVAILABLE")
                self.validator.validate(spec.input, artifact.input_schema)
            except JSONValidationUnavailable as exc:
                raise TaskValidationUnavailable(exc.code) from None
            except (JSONValidationError, RecursionError):
                raise TaskError("Task input or published input schema is invalid") from None
            return PreparedTask(request, target=target, recovery=recovery)
        except (TaskError, TaskValidationUnavailable) as exc:
            return PreparedTask(request, failure=exc, recovery=recovery)

    def submit(self, principal, project_key, spec: TaskSubmit, idempotency_key) -> tuple[TaskRecord, bool]:
        prepared = self.prepare(principal, project_key, spec, idempotency_key)
        with self.admission.unit_of_work(project_key, self.owner(principal)) as admission:
            return admission.admit(prepared)

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

        source = self._recovery_source(principal, project_key, task_id)
        task = source.task()
        artifact = self.admission.target(project_key, task.capability_key, task.version).artifact()
        return assess_workflow_recovery(task, artifact, artifact.content_hash)

    def _recovery_source(self, principal, project_key, task_id):
        allowed = self._access(principal, project_key, "task:read")
        return self.admission.recovery_source(
            project_key, task_id, allowed_keys=allowed,
            owner=None if principal.has_scope("admin") else self.owner(principal))

    def prepare_recovery(self, principal, project_key, task_id, request_key):
        source = self._recovery_source(principal, project_key, task_id)
        task = source.task()
        key = "recover:" + hashlib.sha256(f"{task_id}:{request_key}".encode()).hexdigest()
        return self.prepare(principal, project_key, TaskSubmit(capability_key=task.capability_key,
            version=task.version, business_id=task.business_id, customer_id=task.customer_id,
            input=source.input()), key, recovery=source)

    def recover(self, principal, project_key, task_id, request_key) -> tuple[TaskRecord, bool]:
        prepared = self.prepare_recovery(principal, project_key, task_id, request_key)
        with self.admission.unit_of_work(project_key, self.owner(principal)) as admission:
            return admission.admit(prepared)
