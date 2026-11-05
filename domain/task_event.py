"""Content-free, committed Task milestones with a task-local ordered sequence."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    sequence: int = Field(ge=1, le=9007199254740991)
    task_attempt_id: str | None
    entity_kind: Literal["task", "workflow_node", "agent_execution", "model_call", "tool_call"]
    entity_id: str
    event_type: str
    occurred_at: datetime


class TaskEventPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[TaskEventRecord, ...]
    head_sequence: int
    next_after: int
    has_more: bool
    history_complete: bool
