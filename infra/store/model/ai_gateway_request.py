"""Immutable accepted GatewayRequest evidence."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKeyConstraint, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from infra.store.model.base import PlatformBase as Base
from infra.store.model.base import UTCDateTime


class AiGatewayRequest(Base):
    __tablename__ = "ai_gateway_request"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_by: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_mode: Mapped[str] = mapped_column(Text, nullable=False)
    task_created: Mapped[bool] = mapped_column(Boolean, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["task_id", "project_key"], ["ai_task.id", "ai_task.project_key"],
                             name="fk_ai_gateway_request_task_project"),
        CheckConstraint("id ~ '^[0-9a-f]{32}$'", name="ck_ai_gateway_request_id"),
        CheckConstraint("project_key ~ '^[a-z][a-z0-9_.-]{0,99}$'", name="ck_ai_gateway_request_project_key"),
        CheckConstraint("length(task_id) BETWEEN 1 AND 200", name="ck_ai_gateway_request_task_id"),
        CheckConstraint("length(submitted_by) BETWEEN 1 AND 256", name="ck_ai_gateway_request_submitted_by"),
        CheckConstraint("operation IN ('submit','execute','recover')", name="ck_ai_gateway_request_operation"),
        CheckConstraint("delivery_mode IN ('sync','async')", name="ck_ai_gateway_request_delivery_mode"),
        CheckConstraint("operation = 'execute' OR delivery_mode = 'async'",
                        name="ck_ai_gateway_request_sync_execute"),
        CheckConstraint("correlation_id IS NULL OR correlation_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
                        name="ck_ai_gateway_request_correlation_id"),
        Index("ix_ai_gateway_request_project_task_created_id", "project_key", "task_id", "create_time", "id"),
    )
