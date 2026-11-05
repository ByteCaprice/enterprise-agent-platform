"""Project tool bindings and content-free, node/lease-scoped call evidence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, ForeignKeyConstraint, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain.platform_tool import PlatformToolCallRecord, ToolBindingRecord
from infra.store.model.base import PlatformBase as Base
from infra.store.model.base import UTCDateTime


class AiPlatformToolBinding(Base):
    __tablename__ = "ai_platform_tool_binding"

    project_key: Mapped[str] = mapped_column(ForeignKey("ai_project.key"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    handler_key: Mapped[str] = mapped_column(Text, nullable=False)
    handler_version: Mapped[str] = mapped_column(Text, nullable=False)
    operation_type: Mapped[str] = mapped_column(Text, nullable=False)
    max_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("operation_type = 'read_only'", name="ck_ai_platform_tool_binding_operation"),
        CheckConstraint("max_timeout_seconds > 0 AND max_timeout_seconds <= 300", name="ck_ai_platform_tool_binding_timeout"),
    )

    def to_record(self) -> ToolBindingRecord:
        return ToolBindingRecord.model_validate({name: getattr(self, name) for name in ToolBindingRecord.model_fields})


class AiPlatformToolCall(Base):
    __tablename__ = "ai_platform_tool_call"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_attempt_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text)
    agent_execution_id: Mapped[str | None] = mapped_column(Text)
    agent_step_id: Mapped[str | None] = mapped_column(Text)
    agent_step_kind: Mapped[str | None] = mapped_column(Text)
    tool_capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_key: Mapped[str] = mapped_column(Text, nullable=False)
    tool_version: Mapped[str] = mapped_column(Text, nullable=False)
    tool_hash: Mapped[str] = mapped_column(Text, nullable=False)
    binding_key: Mapped[str] = mapped_column(Text, nullable=False)
    handler_key: Mapped[str] = mapped_column(Text, nullable=False)
    handler_version: Mapped[str] = mapped_column(Text, nullable=False)
    operation_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    output_hash: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        ForeignKeyConstraint(["task_id", "project_key"], ["ai_task.id", "ai_task.project_key"], name="fk_ai_platform_tool_call_task_project"),
        ForeignKeyConstraint(["task_id", "task_attempt_id", "node_id"], ["ai_workflow_node.task_id", "ai_workflow_node.task_attempt_id", "ai_workflow_node.id"], name="fk_ai_platform_tool_call_node"),
        ForeignKeyConstraint(["project_key", "binding_key"], ["ai_platform_tool_binding.project_key", "ai_platform_tool_binding.key"], name="fk_ai_platform_tool_call_binding"),
        ForeignKeyConstraint(["tool_capability_id", "project_key"], ["ai_capability.id", "ai_capability.project_key"], name="fk_ai_platform_tool_call_capability_project"),
        ForeignKeyConstraint(["tool_capability_id", "tool_version"], ["ai_capability_version.capability_id", "ai_capability_version.version"], name="fk_ai_platform_tool_call_version"),
        UniqueConstraint("node_id", name="uk_ai_platform_tool_call_node"),
        ForeignKeyConstraint(["task_id", "task_attempt_id", "agent_execution_id", "agent_step_id", "agent_step_kind"], ["ai_agent_step.task_id", "ai_agent_step.task_attempt_id", "ai_agent_step.agent_execution_id", "ai_agent_step.id", "ai_agent_step.kind"], name="fk_ai_platform_tool_call_agent_step"),
        CheckConstraint("(node_id IS NOT NULL AND agent_execution_id IS NULL AND agent_step_id IS NULL AND agent_step_kind IS NULL) OR "
                        "(node_id IS NULL AND agent_execution_id IS NOT NULL AND agent_step_id IS NOT NULL AND agent_step_kind IS NOT NULL AND agent_step_kind='tool_call')", name="ck_ai_platform_tool_call_owner"),
        UniqueConstraint("agent_step_id", name="uk_ai_platform_tool_call_step"),
        CheckConstraint("operation_type = 'read_only'", name="ck_ai_platform_tool_call_operation"),
        CheckConstraint("status IN ('running','succeeded','failed','cancelled')", name="ck_ai_platform_tool_call_status"),
        CheckConstraint("(status = 'running' AND finish_time IS NULL AND output_hash IS NULL AND error_code IS NULL) OR "
                        "(status = 'succeeded' AND finish_time IS NOT NULL AND output_hash IS NOT NULL AND output_hash ~ '^[0-9a-f]{64}$' AND error_code IS NULL) OR "
                        "(status IN ('failed','cancelled') AND finish_time IS NOT NULL AND output_hash IS NULL AND error_code IS NOT NULL AND error_code ~ '^TOOL_[A-Z0-9_]{1,90}$')",
                        name="ck_ai_platform_tool_call_terminal"),
        CheckConstraint("finish_time IS NULL OR finish_time >= start_time", name="ck_ai_platform_tool_call_time"),
        CheckConstraint("tool_hash ~ '^[0-9a-f]{64}$' AND input_hash ~ '^[0-9a-f]{64}$'", name="ck_ai_platform_tool_call_hashes"),
        Index("idx_ai_platform_tool_call_owner", "project_key", "task_id", "start_time"),
    )

    def to_record(self) -> PlatformToolCallRecord:
        return PlatformToolCallRecord.model_validate({name: getattr(self, name) for name in PlatformToolCallRecord.model_fields})
