"""Execution lease state for a single task attempt."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from domain.task_execution import TaskAttemptRecord
from infra.store.model.base import PlatformBase as Base
from infra.store.model.base import UTCDateTime


class AiTaskAttempt(Base):
    __tablename__ = "ai_task_attempt"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    heartbeat_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_code: Mapped[str | None] = mapped_column(Text)
    cancel_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        ForeignKeyConstraint(["task_id"], ["ai_task.id"], name="fk_ai_task_attempt_task"),
        UniqueConstraint("task_id", "number", name="uk_ai_task_attempt_number"),
        UniqueConstraint("task_id", "id", name="uk_ai_task_attempt_task_id"),
        CheckConstraint(
            "number > 0 AND length(worker_id) BETWEEN 1 AND 200 AND token ~ '^[0-9a-f]{64}$'",
            name="ck_ai_task_attempt_identity",
        ),
        CheckConstraint("status IN ('running','succeeded','failed','timed_out','cancelled')", name="ck_ai_task_attempt_status"),
        CheckConstraint(
            "(status = 'running' AND finish_time IS NULL) OR (status <> 'running' AND finish_time IS NOT NULL AND finish_time >= start_time)",
            name="ck_ai_task_attempt_finish",
        ),
        CheckConstraint(
            "lease_expires_at >= start_time AND heartbeat_time >= start_time", name="ck_ai_task_attempt_times"
        ),
        CheckConstraint(
            "(status IN ('failed','timed_out') AND error_code IS NOT NULL AND error_code ~ '^[A-Z][A-Z0-9_]{0,99}$') OR "
            "(status IN ('running','succeeded','cancelled') AND error_code IS NULL)",
            name="ck_ai_task_attempt_error_code",
        ),
        CheckConstraint("(cancel_observed_at IS NULL OR cancel_observed_at >= start_time) AND "
                        "(cancel_acknowledged_at IS NULL OR (cancel_observed_at IS NOT NULL AND "
                        "cancel_acknowledged_at >= cancel_observed_at AND status='cancelled' AND "
                        "cancel_acknowledged_at=finish_time)) AND "
                        "(status != 'cancelled' OR cancel_acknowledged_at IS NOT NULL)", name="ck_ai_task_attempt_cancellation"),
        Index("idx_ai_task_attempt_task", "task_id", "number"),
        Index("idx_ai_task_attempt_expiry", "lease_expires_at", postgresql_where=text("status = 'running'")),
        Index("uk_ai_task_attempt_running", "task_id", unique=True, postgresql_where=text("status = 'running'")),
    )

    def to_domain(self) -> TaskAttemptRecord:
        return TaskAttemptRecord.model_validate(
            {field: getattr(self, field) for field in TaskAttemptRecord.model_fields}
        )
