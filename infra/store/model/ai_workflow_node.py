"""Task-attempt scoped Workflow checkpoints, separate from task terminal state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from domain.workflow import WorkflowNodeRecord
from infra.store.model.base import JSON_VARIANT, UTCDateTime
from infra.store.model.base import PlatformBase as Base


class AiWorkflowNode(Base):
    __tablename__ = "ai_workflow_node"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_attempt_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    output_hash: Mapped[str | None] = mapped_column(Text)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON_VARIANT)
    reused_from_node_id: Mapped[str | None] = mapped_column(ForeignKey("ai_workflow_node.id"))
    start_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_code: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "task_attempt_id"],
            ["ai_task_attempt.task_id", "ai_task_attempt.id"],
            name="fk_ai_workflow_node_attempt",
        ),
        UniqueConstraint("task_attempt_id", "node_key", name="uk_ai_workflow_node_attempt_key"),
        UniqueConstraint("task_id", "task_attempt_id", "id", name="uk_ai_workflow_node_task_attempt_id"),
        CheckConstraint("kind IN ('rule','agent','tool','llm')", name="ck_ai_workflow_node_kind"),
        CheckConstraint("status IN ('running','succeeded','failed','skipped','cancelled')", name="ck_ai_workflow_node_status"),
        CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$' AND (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$')",
            name="ck_ai_workflow_node_hashes",
        ),
        CheckConstraint(
            "(status = 'running' AND finish_time IS NULL AND output IS NULL AND output_hash IS NULL AND error_code IS NULL) OR (status = 'succeeded' AND finish_time IS NOT NULL AND output IS NOT NULL AND output_hash IS NOT NULL AND error_code IS NULL) OR (status = 'skipped' AND finish_time IS NOT NULL AND output IS NULL AND output_hash IS NULL AND error_code IS NULL) OR (status IN ('failed','cancelled') AND finish_time IS NOT NULL AND output IS NULL AND output_hash IS NULL AND error_code IS NOT NULL AND error_code ~ '^[A-Z][A-Z0-9_]{0,99}$')",
            name="ck_ai_workflow_node_terminal",
        ),
        CheckConstraint("finish_time IS NULL OR finish_time >= start_time", name="ck_ai_workflow_node_time"),
        CheckConstraint("output IS NULL OR jsonb_typeof(output) = 'object'", name="ck_ai_workflow_node_output"),
        CheckConstraint("reused_from_node_id IS NULL OR (kind='rule' AND status IN ('succeeded','skipped'))",
                        name="ck_ai_workflow_node_reused"),
        CheckConstraint("node_key ~ '^[a-z][a-z0-9_.-]{0,99}$'", name="ck_ai_workflow_node_key"),
        Index("idx_ai_workflow_node_task", "task_id", "task_attempt_id", "start_time", "node_key"),
        Index("idx_ai_workflow_node_running", "task_attempt_id", postgresql_where=text("status = 'running'")),
    )

    def to_record(self) -> WorkflowNodeRecord:
        return WorkflowNodeRecord.model_validate(
            {name: getattr(self, name) for name in WorkflowNodeRecord.model_fields}
        )
