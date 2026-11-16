"""New-platform read-only tool contracts; never import executable paths from data."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from domain.business_code import CodeReference
from domain.capability import Key, Version
from domain.governance import GovernanceInput
from domain.platform_agent import CallOwner, CallOwnership


class ToolConfigurationError(ValueError):
    pass


class ToolBindingNotFound(ToolConfigurationError):
    pass


class ToolConflict(ToolConfigurationError):
    pass


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    definition_version: Literal["tool.v1", "tool.code.v1"] = "tool.v1"
    binding_key: Key
    operation_type: Literal["read_only"]
    timeout_seconds: float = Field(default=30, strict=True, gt=0, le=300)
    implementation: CodeReference | None = None

    @model_validator(mode="after")
    def explicit_implementation(self):
        if (self.definition_version == "tool.code.v1") != (self.implementation is not None):
            raise ValueError("Only tool.code.v1 requires a pinned code implementation")
        return self


class ToolBindingCreate(GovernanceInput):
    key: Key
    name: str = Field(min_length=1, max_length=200)
    handler_key: Key
    handler_version: Version
    operation_type: Literal["read_only"]
    max_timeout_seconds: float = Field(default=30, strict=True, gt=0, le=300)


class ToolBindingRecord(ToolBindingCreate):
    project_key: str
    enabled: bool
    created_by: str
    create_time: datetime


@dataclass(frozen=True)
class ToolContext:
    """Trusted scope passed separately from user arguments; no credential or lease token."""

    project_key: str
    submitted_by: str
    task_id: str
    task_attempt_id: str
    node_id: str | None
    tool_key: str
    tool_version: str
    agent_execution_id: str | None = None
    agent_step_id: str | None = None

    def __post_init__(self):
        CallOwnership(node_id=self.node_id, agent_execution_id=self.agent_execution_id, agent_step_id=self.agent_step_id)

    @property
    def owner(self) -> CallOwner:
        return CallOwnership(node_id=self.node_id, agent_execution_id=self.agent_execution_id,
                             agent_step_id=self.agent_step_id).owner


@dataclass(frozen=True)
class InstalledReadonlyTool:
    """Operator-reviewed async code installed at deployment, not via the capability API.

    This is a code trust boundary, not a Python sandbox or proof of no side effects.
    Implementations must honor cancellation and propagate the supplied trusted scope.
    """

    key: str
    version: str
    execute: Callable[[ToolContext, dict], Awaitable[dict]] = field(repr=False)
    implementation: CodeReference | None = None

    def __post_init__(self):
        TypeAdapter(Key).validate_python(self.key)
        TypeAdapter(Version).validate_python(self.version)
        if not inspect.iscoroutinefunction(self.execute):
            raise ValueError("Install an explicit async, cancellation-cooperative tool handler")
        if self.implementation is not None:
            if not isinstance(self.implementation, CodeReference):
                raise ValueError("Install an explicit CodeReference for the tool handler")
            reference = CodeReference.model_validate(self.implementation.model_dump())
            if (reference.key, reference.version) != (self.key, self.version):
                raise ValueError("Tool implementation must pin the installed handler key/version")
            object.__setattr__(self, "implementation", reference)


class PlatformToolCallRecord(CallOwnership):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    task_id: str
    task_attempt_id: str
    tool_key: str
    tool_version: str
    tool_hash: str
    binding_key: str
    handler_key: str
    handler_version: str
    operation_type: Literal["read_only"]
    status: Literal["running", "succeeded", "failed", "cancelled"]
    input_hash: str
    output_hash: str | None = None
    error_code: str | None = None
    start_time: datetime
    finish_time: datetime | None = None
