"""Subordinate Agent execution/step evidence; the TaskAttempt owns the only lease."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from domain.platform_agent import AgentExecutionRecord, AgentStepRecord
from infra.store.model.base import PlatformBase as Base
from infra.store.model.base import UTCDateTime


class AiAgentExecution(Base):
    __tablename__ = "ai_agent_execution"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_attempt_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_key: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str] = mapped_column(Text, nullable=False)
    agent_hash: Mapped[str] = mapped_column(Text, nullable=False)
    parent_node_id: Mapped[str | None] = mapped_column(Text)
    runtime_profile: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    output_hash: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        ForeignKeyConstraint(["task_id", "project_key"], ["ai_task.id", "ai_task.project_key"], name="fk_ai_agent_execution_task"),
        ForeignKeyConstraint(["task_id", "task_attempt_id"], ["ai_task_attempt.task_id", "ai_task_attempt.id"], name="fk_ai_agent_execution_attempt"),
        ForeignKeyConstraint(["task_id", "task_attempt_id", "parent_node_id"], ["ai_workflow_node.task_id", "ai_workflow_node.task_attempt_id", "ai_workflow_node.id"], name="fk_ai_agent_execution_node"),
        ForeignKeyConstraint(["agent_capability_id", "project_key"], ["ai_capability.id", "ai_capability.project_key"], name="fk_ai_agent_execution_project"),
        ForeignKeyConstraint(["agent_capability_id", "agent_version"], ["ai_capability_version.capability_id", "ai_capability_version.version"], name="fk_ai_agent_execution_version"),
        UniqueConstraint("project_key", "task_id", "task_attempt_id", "id", name="uk_ai_agent_execution_scope"),
        Index("uk_ai_agent_execution_root", "task_attempt_id", unique=True, postgresql_where=text("parent_node_id IS NULL")),
        Index("ix_ai_agent_execution_running_capability", "agent_capability_id",
              postgresql_where=text("parent_node_id IS NOT NULL AND status = 'running'")),
        UniqueConstraint("parent_node_id", name="uk_ai_agent_execution_node"),
        CheckConstraint("agent_hash ~ '^[0-9a-f]{64}$'", name="ck_ai_agent_execution_hash"),
        CheckConstraint("status IN ('running','succeeded','failed','cancelled')", name="ck_ai_agent_execution_status"),
        CheckConstraint("(status='running' AND finish_time IS NULL AND output_hash IS NULL AND error_code IS NULL) OR "
                        "(status='succeeded' AND finish_time IS NOT NULL AND output_hash IS NOT NULL AND output_hash ~ '^[0-9a-f]{64}$' AND error_code IS NULL) OR "
                        "(status IN ('failed','cancelled') AND finish_time IS NOT NULL AND output_hash IS NULL AND error_code IS NOT NULL AND error_code ~ '^AGENT_[A-Z0-9_]{1,90}$')", name="ck_ai_agent_execution_terminal"),
        CheckConstraint("finish_time IS NULL OR finish_time >= start_time", name="ck_ai_agent_execution_time"),
    )

    def to_record(self):
        return AgentExecutionRecord.model_validate({name: getattr(self, name) for name in AgentExecutionRecord.model_fields})


class AiAgentStep(Base):
    __tablename__ = "ai_agent_step"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_attempt_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_execution_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    resource_capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    resource_key: Mapped[str] = mapped_column(Text, nullable=False)
    resource_version: Mapped[str] = mapped_column(Text, nullable=False)
    resource_hash: Mapped[str] = mapped_column(Text, nullable=False)
    source_model_step_id: Mapped[str | None] = mapped_column(Text)
    source_model_kind: Mapped[str | None] = mapped_column(Text)
    dispatch_id: Mapped[str | None] = mapped_column(Text, unique=True)
    tool_alias: Mapped[str | None] = mapped_column(Text)
    source_call_id_hash: Mapped[str | None] = mapped_column(Text)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    output_hash: Mapped[str | None] = mapped_column(Text)
    final_result_hash: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    start_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        ForeignKeyConstraint(["project_key", "task_id", "task_attempt_id", "agent_execution_id"], ["ai_agent_execution.project_key", "ai_agent_execution.task_id", "ai_agent_execution.task_attempt_id", "ai_agent_execution.id"], name="fk_ai_agent_step_execution"),
        ForeignKeyConstraint(["resource_capability_id", "project_key"], ["ai_capability.id", "ai_capability.project_key"], name="fk_ai_agent_step_project"),
        ForeignKeyConstraint(["resource_capability_id", "resource_version"], ["ai_capability_version.capability_id", "ai_capability_version.version"], name="fk_ai_agent_step_version"),
        UniqueConstraint("task_id", "task_attempt_id", "agent_execution_id", "id", "kind", name="uk_ai_agent_step_owner"),
        UniqueConstraint("agent_execution_id", "turn", "id", "kind", name="uk_ai_agent_step_source"),
        ForeignKeyConstraint(["agent_execution_id", "turn", "source_model_step_id", "source_model_kind"], ["ai_agent_step.agent_execution_id", "ai_agent_step.turn", "ai_agent_step.id", "ai_agent_step.kind"], name="fk_ai_agent_step_source"),
        UniqueConstraint("agent_execution_id", "turn", "kind", "slot", name="uk_ai_agent_step_slot"),
        UniqueConstraint("source_model_step_id", "source_call_id_hash", name="uk_ai_agent_step_source_call"),
        CheckConstraint("kind IN ('model_turn','tool_call') AND turn BETWEEN 1 AND 128 AND slot BETWEEN 0 AND 511", name="ck_ai_agent_step_kind"),
        CheckConstraint("(kind='model_turn' AND slot=0 AND source_model_step_id IS NULL AND source_model_kind IS NULL AND dispatch_id IS NULL AND tool_alias IS NULL AND source_call_id_hash IS NULL) OR "
                        "(kind='tool_call' AND source_model_step_id IS NOT NULL AND source_model_kind IS NOT NULL AND source_model_kind='model_turn' AND dispatch_id IS NOT NULL AND tool_alias IS NOT NULL AND source_call_id_hash IS NOT NULL AND source_call_id_hash ~ '^[0-9a-f]{64}$')", name="ck_ai_agent_step_source"),
        CheckConstraint("status IN ('pending','running','succeeded','failed','cancelled') AND (kind='tool_call' OR status!='pending')", name="ck_ai_agent_step_status"),
        CheckConstraint("(status='pending' AND start_time IS NULL AND finish_time IS NULL AND output_hash IS NULL AND error_code IS NULL) OR "
                        "(status='running' AND start_time IS NOT NULL AND finish_time IS NULL AND output_hash IS NULL AND error_code IS NULL) OR "
                        "(status='succeeded' AND start_time IS NOT NULL AND finish_time IS NOT NULL AND output_hash IS NOT NULL AND output_hash ~ '^[0-9a-f]{64}$' AND error_code IS NULL) OR "
                        "(status IN ('failed','cancelled') AND finish_time IS NOT NULL AND output_hash IS NULL AND error_code IS NOT NULL AND error_code ~ '^AGENT_[A-Z0-9_]{1,90}$')", name="ck_ai_agent_step_terminal"),
        CheckConstraint("resource_hash ~ '^[0-9a-f]{64}$' AND input_hash ~ '^[0-9a-f]{64}$'", name="ck_ai_agent_step_hashes"),
        CheckConstraint("(kind='tool_call' AND decision IS NULL AND final_result_hash IS NULL) OR "
                        "(kind='model_turn' AND status!='succeeded' AND decision IS NULL AND final_result_hash IS NULL) OR "
                        "(kind='model_turn' AND status='succeeded' AND decision IS NOT NULL AND "
                        "((decision='tools' AND final_result_hash IS NULL) OR (decision='final' AND final_result_hash IS NOT NULL AND final_result_hash ~ '^[0-9a-f]{64}$')))", name="ck_ai_agent_step_decision"),
        CheckConstraint("(start_time IS NULL OR start_time>=create_time) AND (finish_time IS NULL OR finish_time>=COALESCE(start_time,create_time))", name="ck_ai_agent_step_time"),
        Index("idx_ai_agent_step_execution", "agent_execution_id", "turn", "kind", "slot"),
    )

    def to_record(self):
        return AgentStepRecord.model_validate({name: getattr(self, name) for name in AgentStepRecord.model_fields})
