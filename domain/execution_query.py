"""Public execution read models, separate from runtime dispatch and lease ports."""

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from domain.platform_agent import CallOwnership
from domain.task import TaskRecord
from domain.task_execution import TaskAttemptRecord
from domain.workflow import WorkflowNodeRecord


class AgentExecutionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    project_key: str
    task_id: str
    task_attempt_id: str
    agent_key: str
    agent_version: str
    agent_hash: str
    parent_node_id: str | None
    runtime_profile: str
    status: Literal["running", "succeeded", "failed", "cancelled"]
    output_hash: str | None
    error_code: str | None
    start_time: datetime
    finish_time: datetime | None


class AgentStepView(BaseModel):
    """Allowlisted evidence: no dispatch handle, source call ID or argument body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    project_key: str
    task_id: str
    task_attempt_id: str
    agent_execution_id: str
    kind: Literal["model_turn", "tool_call"]
    turn: int
    slot: int
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    resource_key: str
    resource_version: str
    resource_hash: str
    source_model_step_id: str | None
    tool_alias: str | None
    input_hash: str
    output_hash: str | None
    final_result_hash: str | None
    decision: Literal["tools", "final"] | None
    error_code: str | None
    create_time: datetime
    start_time: datetime | None
    finish_time: datetime | None


class ModelCallView(CallOwnership):
    """Tree accounting, without provider request IDs, transport or prompt bodies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    task_id: str
    task_attempt_id: str
    policy_key: str
    policy_version: str
    policy_hash: str
    status: Literal["running", "succeeded", "failed", "cancelled"]
    input_hash: str
    output_hash: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    error_code: str | None
    start_time: datetime
    finish_time: datetime | None


class ToolCallView(CallOwnership):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    task_id: str
    task_attempt_id: str
    tool_key: str
    tool_version: str
    tool_hash: str
    status: Literal["running", "succeeded", "failed", "cancelled"]
    input_hash: str
    output_hash: str | None
    error_code: str | None
    start_time: datetime
    finish_time: datetime | None


ExecutionKind = Literal["attempt", "workflow_node", "agent_execution", "agent_step", "model_call", "tool_call"]


class ExecutionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ExecutionKind
    id: str


class _TreeItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent: ExecutionRef


class WorkflowNodeItem(_TreeItem):
    kind: Literal["workflow_node"] = "workflow_node"
    record: WorkflowNodeRecord


class AgentExecutionItem(_TreeItem):
    kind: Literal["agent_execution"] = "agent_execution"
    record: AgentExecutionView


class AgentStepItem(_TreeItem):
    kind: Literal["agent_step"] = "agent_step"
    record: AgentStepView


class ModelCallItem(_TreeItem):
    kind: Literal["model_call"] = "model_call"
    record: ModelCallView


class ToolCallItem(_TreeItem):
    kind: Literal["tool_call"] = "tool_call"
    record: ToolCallView


ExecutionTreeItem = Annotated[
    WorkflowNodeItem | AgentExecutionItem | AgentStepItem | ModelCallItem | ToolCallItem,
    Field(discriminator="kind"),
]

T = TypeVar("T")


class MetadataPage(BaseModel, Generic[T]):
    """One request snapshot; a cursor is a position, not a retained DB snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[T, ...]
    next_cursor: str | None
    read_time: datetime
    consistency: Literal["page_snapshot"] = "page_snapshot"


class ExecutionTreePage(MetadataPage[ExecutionTreeItem]):
    task: TaskRecord
    attempt: TaskAttemptRecord
