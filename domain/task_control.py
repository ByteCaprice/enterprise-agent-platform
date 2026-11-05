"""Durable stop intent and lease-owner observations, not another task scheduler."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from domain.task import TaskRecord


class ControlState(StrEnum):
    ACTIVE = "active"
    CANCELLATION_REQUESTED = "cancellation_requested"
    AUTHORIZATION_REVOKED = "authorization_revoked"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True)
class ExecutionObservation:
    state: ControlState
    cancel_request_id: str | None = None


class CancellationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    requested_at: datetime
    requested_by: str
    state: Literal["requested", "observed", "acknowledged", "unconfirmed"]
    observed_at: datetime | None = None
    acknowledged_at: datetime | None = None


class TaskCancellationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: TaskRecord
    cancellation: CancellationView | None
