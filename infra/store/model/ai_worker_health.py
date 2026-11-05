"""Process-instance heartbeats, never a scheduling lease or task authority."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from infra.store.model.base import JSON_VARIANT, UTCDateTime
from infra.store.model.base import PlatformBase as Base


class AiWorkerHealth(Base):
    __tablename__ = "ai_worker_health"

    instance_id: Mapped[str] = mapped_column(Text, primary_key=True)
    worker_id: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    executors: Mapped[list] = mapped_column(JSON_VARIANT)
    workflow_agents: Mapped[bool] = mapped_column(Boolean)
    state: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (
        CheckConstraint("role IN ('task','callback','recovery') AND state IN ('running','stopped','failed')", name="ck_ai_worker_health"),
        Index("ix_ai_worker_health_live", "state", "heartbeat_at"),
    )
