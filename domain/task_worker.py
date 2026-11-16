"""Explicit execution-plane dependencies; Workers do not need a PlatformStore."""

from dataclasses import dataclass
from typing import Protocol

from domain.execution_authorization import ExecutionAuthorizationPort
from domain.execution_requirements import InstalledExecutionCapabilities
from domain.schema_validation import JSONSchemaValidator
from domain.task_completion import TaskCompletionPort
from domain.task_control import ExecutionObservation
from domain.task_execution import TaskClaim, TaskLease


class TaskSchedulingPort(Protocol):
    def claim(
        self, worker_id: str, *, lease_seconds: float = 30,
        capabilities: InstalledExecutionCapabilities,
    ) -> TaskClaim | None: ...

    def recover_expired(self, *, limit: int = 100) -> int: ...

    def reject_disabled_project_tasks(self, *, limit: int = 100) -> int: ...


class TaskControlPort(Protocol):
    def observe(
        self, lease: TaskLease, *, lease_seconds: float, cancellation_grace_seconds: float,
    ) -> ExecutionObservation: ...

    def acknowledge_cancel(self, lease: TaskLease) -> bool: ...

    def abandon_cancel(self, lease: TaskLease) -> bool: ...


@dataclass(frozen=True)
class WorkerServices:
    """Only execution authority and lifecycle ports, never control-plane storage."""

    queue: TaskSchedulingPort
    control: TaskControlPort
    completion: TaskCompletionPort
    authorization: ExecutionAuthorizationPort
    validation: JSONSchemaValidator
