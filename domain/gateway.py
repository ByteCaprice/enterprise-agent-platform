"""Accepted gateway requests: immutable correlation, never execution authority."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain.capability import Key
from domain.execution_query import MetadataPage
from domain.task import TaskRecord, TaskSubmit
from domain.task_execution import TaskResultRecord


class GatewayExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    task: TaskSubmit
    mode: Literal["sync", "async"] = "async"
    wait_seconds: float | None = Field(default=None, gt=0, le=30)

    @model_validator(mode="after")
    def validate_wait(self):
        if self.mode == "async" and self.wait_seconds is not None:
            raise ValueError("wait_seconds is only available for synchronous requests")
        return self


RequestId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
CorrelationId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
GatewayOperation = Literal["submit", "execute", "recover"]
DeliveryMode = Literal["sync", "async"]


class GatewayRequestContext(BaseModel):
    """Constructed by the transport; request_id is never taken from a client header."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId = Field(default_factory=lambda: uuid4().hex)
    correlation_id: CorrelationId | None = Field(default=None, repr=False)


class GatewayRequestCreate(BaseModel):
    """Trusted server command; no input, idempotency key or credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: RequestId
    project_key: Key
    task_id: str = Field(min_length=1, max_length=200)
    submitted_by: str = Field(min_length=1, max_length=256)
    operation: GatewayOperation
    delivery_mode: DeliveryMode
    task_created: bool
    correlation_id: CorrelationId | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def valid_delivery(self):
        if self.operation != "execute" and self.delivery_mode != "async":
            raise ValueError("Only execute requests can synchronously wait")
        return self


class GatewayRequestRecord(GatewayRequestCreate):
    """One committed admission, including idempotent reuse; timestamp comes from the DB."""

    create_time: datetime


class GatewayRequestPage(MetadataPage[GatewayRequestRecord]):
    pass


class GatewayAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: TaskRecord
    request: GatewayRequestRecord

    @model_validator(mode="after")
    def consistent_binding(self):
        if (self.task.id, self.task.project_key, self.task.submitted_by) != (
            self.request.task_id, self.request.project_key, self.request.submitted_by,
        ):
            raise ValueError("Gateway admission must refer to the same Task and owner")
        return self


class GatewayExecutionResponse(GatewayAdmission):
    result: TaskResultRecord | None = None
    wait_timed_out: bool = False
