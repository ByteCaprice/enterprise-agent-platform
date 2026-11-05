"""Safe metadata contract for conservative Workflow recovery assessment."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

RecoveryReason = Literal[
    "eligible",
    "unsupported_source",
    "source_not_terminal_unsuccessful",
    "artifact_hash_mismatch",
    "invalid_workflow",
    "unsupported_node_kind",
    "retry_policy_unsupported",
]


class WorkflowRecoveryAssessment(BaseModel):
    """Metadata only; eligibility is not a guarantee of checkpoints or runtime access."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_task_id: str
    version: str
    content_hash: str
    eligible: bool
    reason: RecoveryReason
    mode: Literal["new_task"] = "new_task"
    requires_runtime_validation: Literal[True] = True

    @model_validator(mode="after")
    def consistent_eligibility(self):
        if self.eligible != (self.reason == "eligible"):
            raise ValueError("Recovery eligibility must match its reason")
        return self
