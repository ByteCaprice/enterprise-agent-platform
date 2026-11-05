"""Safe, content-free accounting for new-platform workflow model calls."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from domain.platform_model import PlatformModelCallRecord
from infra.store.model.base import PlatformBase as Base
from infra.store.model.base import UTCDateTime


class AiPlatformModelCall(Base):
    __tablename__ = "ai_platform_model_call"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_attempt_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text)
    agent_execution_id: Mapped[str | None] = mapped_column(Text)
    agent_step_id: Mapped[str | None] = mapped_column(Text)
    agent_step_kind: Mapped[str | None] = mapped_column(Text)
    policy_capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    policy_key: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_hash: Mapped[str] = mapped_column(Text, nullable=False)
    provider_key: Mapped[str] = mapped_column(Text, nullable=False)
    requested_model: Mapped[str] = mapped_column(Text, nullable=False)
    actual_model: Mapped[str | None] = mapped_column(Text)
    prompt_key: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    prompt_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    output_hash: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    http_status: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        ForeignKeyConstraint(["task_id", "project_key"], ["ai_task.id", "ai_task.project_key"], name="fk_ai_platform_model_call_task_project"),
        ForeignKeyConstraint(["task_id", "task_attempt_id", "node_id"], ["ai_workflow_node.task_id", "ai_workflow_node.task_attempt_id", "ai_workflow_node.id"], name="fk_ai_platform_model_call_node"),
        ForeignKeyConstraint(["project_key", "provider_key"], ["ai_platform_model_provider.project_key", "ai_platform_model_provider.key"], name="fk_ai_platform_model_call_provider"),
        ForeignKeyConstraint(["policy_capability_id", "project_key"], ["ai_capability.id", "ai_capability.project_key"], name="fk_ai_platform_model_call_policy_project"),
        ForeignKeyConstraint(["policy_capability_id", "policy_version"], ["ai_capability_version.capability_id", "ai_capability_version.version"], name="fk_ai_platform_model_call_policy_version"),
        UniqueConstraint("node_id", "requested_model", name="uk_ai_platform_model_call_node_model"),
        ForeignKeyConstraint(["task_id", "task_attempt_id", "agent_execution_id", "agent_step_id", "agent_step_kind"], ["ai_agent_step.task_id", "ai_agent_step.task_attempt_id", "ai_agent_step.agent_execution_id", "ai_agent_step.id", "ai_agent_step.kind"], name="fk_ai_platform_model_call_agent_step"),
        CheckConstraint("(node_id IS NOT NULL AND agent_execution_id IS NULL AND agent_step_id IS NULL AND agent_step_kind IS NULL) OR "
                        "(node_id IS NULL AND agent_execution_id IS NOT NULL AND agent_step_id IS NOT NULL AND agent_step_kind IS NOT NULL AND agent_step_kind='model_turn')", name="ck_ai_platform_model_call_owner"),
        UniqueConstraint("agent_step_id", "requested_model", name="uk_ai_platform_model_call_step_model"),
        CheckConstraint("status IN ('running','succeeded','failed','cancelled')", name="ck_ai_platform_model_call_status"),
        CheckConstraint("(status = 'running' AND finish_time IS NULL AND output_hash IS NULL AND error_code IS NULL) OR (status = 'succeeded' AND finish_time IS NOT NULL AND output_hash IS NOT NULL AND output_hash ~ '^[0-9a-f]{64}$' AND actual_model IS NOT NULL AND error_code IS NULL) OR (status IN ('failed','cancelled') AND finish_time IS NOT NULL AND output_hash IS NULL AND error_code IS NOT NULL AND error_code ~ '^MODEL_[A-Z0-9_]{1,90}$')", name="ck_ai_platform_model_call_terminal"),
        CheckConstraint("finish_time IS NULL OR finish_time >= start_time", name="ck_ai_platform_model_call_time"),
        CheckConstraint("policy_hash ~ '^[0-9a-f]{64}$' AND input_hash ~ '^[0-9a-f]{64}$' AND (prompt_hash IS NULL OR prompt_hash ~ '^[0-9a-f]{64}$')", name="ck_ai_platform_model_call_hashes"),
        CheckConstraint("(prompt_key IS NULL AND prompt_version IS NULL AND prompt_hash IS NULL) OR (prompt_key IS NOT NULL AND prompt_version IS NOT NULL AND prompt_hash IS NOT NULL)", name="ck_ai_platform_model_call_prompt"),
        CheckConstraint("(prompt_tokens IS NULL OR prompt_tokens >= 0) AND (completion_tokens IS NULL OR completion_tokens >= 0) AND (total_tokens IS NULL OR total_tokens >= 0)", name="ck_ai_platform_model_call_usage"),
        CheckConstraint("http_status IS NULL OR http_status BETWEEN 100 AND 599", name="ck_ai_platform_model_call_http"),
        CheckConstraint("provider_request_id IS NULL OR length(provider_request_id) <= 512", name="ck_ai_platform_model_call_request_id"),
        Index("idx_ai_platform_model_call_owner", "project_key", "task_id", "start_time"),
        Index("idx_ai_platform_model_call_node", "task_id", "task_attempt_id", "node_id", "start_time"),
        Index("uk_ai_platform_model_call_running", "node_id", unique=True, postgresql_where=text("status = 'running'")),
        Index("uk_ai_platform_model_call_step_running", "agent_step_id", unique=True, postgresql_where=text("status = 'running'")),
    )

    def to_record(self) -> PlatformModelCallRecord:
        return PlatformModelCallRecord.model_validate({
            name: getattr(self, name) for name in PlatformModelCallRecord.model_fields
        })
