"""Task execution authority, leases, immutable claims and result contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from domain.capability import CapabilityVersionRecord
from domain.execution_context import ExecutionBudget, effective_timeout
from domain.task import TaskRecord


class ExecutionResourceUnstoppable(RuntimeError):
    """An execution resource could not stop; its owning Worker must not claim again."""


class TaskLeaseLost(RuntimeError):
    """Execution no longer has authority to mutate task or node state."""


class TaskCancellationRequested(RuntimeError):
    """Admission is closed, but the current lease may still settle and stop."""


class CompletionOutcome(StrEnum):
    COMPLETED = "completed"
    CANCELLATION_PENDING = "cancellation_pending"
    LEASE_LOST = "lease_lost"


class ExecutionFailure(RuntimeError):
    """An explicitly classified, safe failure from a trusted execution adapter."""

    def __init__(self, code: str, *, status: str = "failed"):
        if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", code) is None or status not in {"failed", "timed_out"}:
            raise ValueError("Invalid execution failure contract")
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class TaskLease:
    task_id: str
    attempt_id: str
    worker_id: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class TaskClaim:
    task: TaskRecord
    lease: TaskLease = field(repr=False)
    input: dict[str, Any] = field(repr=False)
    artifact: CapabilityVersionRecord = field(repr=False)
    budget: ExecutionBudget | None = field(default=None, repr=False)

    def with_timeout(self, seconds: float) -> TaskClaim:
        budget = ExecutionBudget.start(seconds) if self.budget is None else self.budget.child(seconds)
        return replace(self, budget=budget)

    def limit_timeout(self, seconds: float) -> float:
        return effective_timeout(self.budget, seconds)


class TaskAttemptRecord(BaseModel):
    """Public attempt metadata deliberately excludes lease credentials and input/output."""

    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    number: int
    worker_id: str
    status: str
    start_time: datetime
    heartbeat_time: datetime
    lease_expires_at: datetime
    finish_time: datetime | None = None
    error_code: str | None = None
    cancel_observed_at: datetime | None = None
    cancel_acknowledged_at: datetime | None = None


class TaskResultRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: str
    result: dict[str, Any] | None = None
    error_code: str | None = None
