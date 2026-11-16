"""New-platform task lifecycle and separately protected execution result."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKeyConstraint, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from domain.task import TaskRecord
from infra.store.model.base import JSON_VARIANT, UTCDateTime
from infra.store.model.base import PlatformBase as Base


class AiTask(Base):
    __tablename__ = "ai_task"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False)
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    business_id: Mapped[str | None] = mapped_column(Text)
    customer_id: Mapped[str | None] = mapped_column(Text)
    submitted_by: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_digest: Mapped[str] = mapped_column(Text, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Non-success has no result (SQL NULL), not a JSON value of null. Keep this
    # distinction on ORM updates as well as initial admission/raw SQL writes.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_VARIANT(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    cancel_request_id: Mapped[str | None] = mapped_column(Text)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_requested_by: Mapped[str | None] = mapped_column(Text)
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    event_history_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    origin_task_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(["origin_task_id", "project_key"], ["ai_task.id", "ai_task.project_key"], name="fk_ai_task_origin"),
        ForeignKeyConstraint(["capability_id", "project_key"], ["ai_capability.id", "ai_capability.project_key"],
                             name="fk_ai_task_project_capability"),
        ForeignKeyConstraint(["capability_id", "version"], ["ai_capability_version.capability_id", "ai_capability_version.version"],
                             name="fk_ai_task_version"),
        UniqueConstraint("project_key", "submitted_by", "idempotency_digest", name="uk_ai_task_idempotency"),
        UniqueConstraint("id", "project_key", name="uk_ai_task_id_project"),
        CheckConstraint("status IN ('queued','running','succeeded','failed','timed_out','cancelled')", name="ck_ai_task_status"),
        CheckConstraint("(status IN ('queued','running') AND finish_time IS NULL) OR "
                        "(status IN ('cancelled','succeeded','failed','timed_out') AND finish_time IS NOT NULL AND finish_time >= create_time)",
                        name="ck_ai_task_finish"),
        CheckConstraint("(status IN ('failed','timed_out') AND error_code IS NOT NULL AND error_code ~ '^[A-Z][A-Z0-9_]{0,99}$') OR "
                        "(status NOT IN ('failed','timed_out') AND error_code IS NULL)", name="ck_ai_task_error_code"),
        CheckConstraint("(status = 'succeeded' AND result IS NOT NULL AND jsonb_typeof(result) = 'object') OR "
                        "(status <> 'succeeded' AND result IS NULL)", name="ck_ai_task_result"),
        CheckConstraint("idempotency_digest ~ '^[0-9a-f]{64}$' AND request_fingerprint ~ '^[0-9a-f]{64}$' "
                        "AND content_hash ~ '^[0-9a-f]{64}$'", name="ck_ai_task_hashes"),
        CheckConstraint("jsonb_typeof(input) = 'object'", name="ck_ai_task_input"),
        CheckConstraint("(cancel_request_id IS NULL AND cancel_requested_at IS NULL AND cancel_requested_by IS NULL) OR "
                        "(cancel_request_id IS NOT NULL AND cancel_request_id ~ '^[0-9a-f]{32}$' AND "
                        "cancel_requested_at IS NOT NULL AND cancel_requested_at >= create_time AND "
                        "cancel_requested_by IS NOT NULL AND length(cancel_requested_by) BETWEEN 1 AND 200 AND "
                        "status IN ('running','cancelled','failed','timed_out') AND "
                        "(finish_time IS NULL OR finish_time >= cancel_requested_at))", name="ck_ai_task_cancellation"),
        CheckConstraint("status != 'cancelled' OR cancel_request_id IS NOT NULL", name="ck_ai_task_cancelled_intent"),
        CheckConstraint("event_sequence BETWEEN 0 AND 9007199254740991", name="ck_ai_task_event_sequence"),
        Index("idx_ai_task_owner_created", "project_key", "submitted_by", "create_time", "id"),
        Index("ix_ai_task_project_status", "project_key", "status", "create_time", "id"),
        Index("idx_ai_task_queued", "create_time", "id", postgresql_where=text("status = 'queued'")),
        Index("ix_ai_task_business", "project_key", "business_id", "create_time", "id",
              postgresql_where=text("business_id IS NOT NULL")),
        Index("ix_ai_task_customer", "project_key", "customer_id", "create_time", "id",
              postgresql_where=text("customer_id IS NOT NULL")),
        Index("ix_ai_task_running_capability", "capability_id", postgresql_where=text("status = 'running'")),
    )

    def to_domain(self, capability_key, capability_kind) -> TaskRecord:
        return TaskRecord(
            **{field: getattr(self, field) for field in TaskRecord.model_fields
               if field not in {"capability_key", "capability_kind"}},
            capability_key=capability_key, capability_kind=capability_kind,
        )
