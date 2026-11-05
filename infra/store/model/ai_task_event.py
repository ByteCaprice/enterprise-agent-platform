"""Durable Task event projection; deliberately no free-form JSON or payload body."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from infra.store.model.base import PlatformBase as Base
from infra.store.model.base import UTCDateTime


class AiTaskEvent(Base):
    __tablename__ = "ai_task_event"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False)
    task_attempt_id: Mapped[str | None] = mapped_column(Text)
    entity_kind: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["task_id", "project_key"], ["ai_task.id", "ai_task.project_key"], name="fk_ai_task_event_task"),
        ForeignKeyConstraint(["task_id", "task_attempt_id"], ["ai_task_attempt.task_id", "ai_task_attempt.id"], name="fk_ai_task_event_attempt"),
        CheckConstraint("sequence BETWEEN 1 AND 9007199254740991", name="ck_ai_task_event_sequence"),
        CheckConstraint("entity_kind IN ('task','workflow_node','agent_execution','model_call','tool_call')", name="ck_ai_task_event_kind"),
        CheckConstraint("length(entity_id) BETWEEN 1 AND 200 AND event_type ~ '^[a-z][a-z0-9_]{0,99}$'", name="ck_ai_task_event_type"),
    )
