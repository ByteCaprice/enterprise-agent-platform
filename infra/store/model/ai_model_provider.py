"""Project-scoped, immutable provider endpoint and model catalog."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from domain.platform_model import ModelProviderRecord
from infra.store.model.base import PlatformBase as Base
from infra.store.model.base import UTCDateTime


class AiPlatformModelProvider(Base):
    __tablename__ = "ai_platform_model_provider"

    project_key: Mapped[str] = mapped_column(ForeignKey("ai_project.key"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    models: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    max_tokens: Mapped[int] = mapped_column(nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("jsonb_typeof(models) = 'array' AND jsonb_array_length(models) BETWEEN 1 AND 100",
                        name="ck_ai_platform_model_provider_models"),
        CheckConstraint("max_tokens BETWEEN 1 AND 1000000", name="ck_ai_platform_model_provider_max_tokens"),
    )

    def to_record(self) -> ModelProviderRecord:
        return ModelProviderRecord.model_validate({
            name: getattr(self, name) for name in ModelProviderRecord.model_fields
        })
