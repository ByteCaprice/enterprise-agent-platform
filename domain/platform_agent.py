"""Governed Agent definition, subordinate evidence metadata and exclusive owners.

Pi execution is explicit opt-in; the default Workflow Worker has no Agent adapter.
See docs/overview.md for the current execution and authorization boundaries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, computed_field, model_validator

from domain.capability import Key, Version

ToolAlias = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ExecutionId = Annotated[str, Field(min_length=1, max_length=200)]


class AgentCapabilityRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Key
    version: Version


class AgentToolBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: ToolAlias
    capability: AgentCapabilityRef


class AgentLimits(BaseModel):
    """Explicit per-execution safety bounds, not monetary budgets or project quotas."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    max_turns: int = Field(strict=True, ge=1, le=128)
    max_tool_calls: int = Field(strict=True, ge=0, le=512)
    timeout_seconds: float = Field(strict=True, gt=0, le=86400)


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition_version: Literal["agent.v1"]
    runtime: Key
    instruction_prompt: AgentCapabilityRef
    model_policy: AgentCapabilityRef
    tools: tuple[AgentToolBinding, ...] = Field(default=(), max_length=64)
    skills: tuple[AgentCapabilityRef, ...] = Field(default=(), max_length=8)
    tool_execution: Literal["sequential"] = "sequential"
    limits: AgentLimits

    @model_validator(mode="after")
    def unambiguous_resources(self):
        aliases = [tool.alias for tool in self.tools]
        if len(aliases) != len(set(aliases)):
            raise ValueError("Agent tool aliases must be unique")
        keys = [reference.key for reference in self.dependency_refs()]
        if len(keys) != len(set(keys)):
            raise ValueError("An Agent must pin each resource key exactly once")
        if bool(self.tools) != (self.limits.max_tool_calls > 0):
            raise ValueError("Tool-call allowance must match the explicit tool catalog")
        return self

    def dependency_refs(self) -> tuple[AgentCapabilityRef, ...]:
        return (self.instruction_prompt, self.model_policy, *(tool.capability for tool in self.tools), *self.skills)


class WorkflowNodeCallOwner(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["workflow_node"]
    node_id: ExecutionId


class AgentStepCallOwner(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["agent_step"]
    agent_execution_id: ExecutionId
    step_id: ExecutionId


CallOwner = Annotated[WorkflowNodeCallOwner | AgentStepCallOwner, Field(discriminator="kind")]


class CallOwnership(BaseModel):
    """Content-free ledger ownership; IDs locate evidence, never authorize a call."""

    node_id: str | None
    agent_execution_id: str | None = None
    agent_step_id: str | None = None

    @model_validator(mode="after")
    def exclusive_owner(self):
        if not ((self.node_id is not None and self.agent_execution_id is None and self.agent_step_id is None) or
                (self.node_id is None and self.agent_execution_id is not None and self.agent_step_id is not None)):
            raise ValueError("A call must have exactly one complete owner")
        return self

    @computed_field
    @property
    def owner(self) -> CallOwner:
        if self.node_id is not None:
            return WorkflowNodeCallOwner(kind="workflow_node", node_id=self.node_id)
        assert self.agent_execution_id is not None and self.agent_step_id is not None
        return AgentStepCallOwner(kind="agent_step", agent_execution_id=self.agent_execution_id, step_id=self.agent_step_id)


class AgentStorageError(ValueError):
    pass


class AgentValidationUnavailable(AgentStorageError):
    """Isolated JSON validation could not complete; retry requires an explicit caller decision."""


class AgentStorageConflict(AgentStorageError):
    pass


class AgentCapacityUnavailable(AgentStorageError):
    """An embedded Agent must wait without starting a model or tool call."""


class AgentToolIntent(BaseModel):
    """Transient accepted-model input; argument bodies are never persisted here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: ToolAlias
    source_call_id: str = Field(min_length=1, max_length=200, repr=False)
    arguments: dict[str, JsonValue] = Field(repr=False)


class AgentExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    task_id: str
    task_attempt_id: str
    agent_key: str
    agent_version: str
    agent_hash: str
    parent_node_id: str | None
    runtime_profile: str
    status: str
    output_hash: str | None
    error_code: str | None
    start_time: datetime
    finish_time: datetime | None


class AgentStepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    task_id: str
    task_attempt_id: str
    agent_execution_id: str
    kind: str
    turn: int
    slot: int
    status: str
    resource_key: str
    resource_version: str
    resource_hash: str
    source_model_step_id: str | None
    dispatch_id: str | None
    tool_alias: str | None
    input_hash: str
    output_hash: str | None
    final_result_hash: str | None
    decision: str | None
    error_code: str | None
    create_time: datetime
    start_time: datetime | None
    finish_time: datetime | None
