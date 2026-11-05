"""Capability identity and immutable published version artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain.capability import CapabilityVersionRecord
from infra.store.model.base import JSON_VARIANT, UTCDateTime
from infra.store.model.base import PlatformBase as Base


class AiCapability(Base):
    __tablename__ = "ai_capability"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(ForeignKey("ai_project.key", name="fk_ai_capability_project"), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    default_version: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_key", "key", name="uk_ai_capability_project_key"),
        UniqueConstraint("id", "project_key", name="uk_ai_capability_id_project"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in (
            "id", "project_key", "key", "kind", "name", "description", "default_version", "created_by", "create_time",
        )}


class AiCapabilityVersion(Base):
    __tablename__ = "ai_capability_version"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    capability_id: Mapped[str] = mapped_column(ForeignKey("ai_capability.id"), nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON_VARIANT, nullable=False)
    dependencies: Mapped[list[Any]] = mapped_column(JSON_VARIANT, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    published_by: Mapped[str | None] = mapped_column(Text)
    publish_time: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        UniqueConstraint("capability_id", "version", name="uk_ai_capability_version"),
        Index("idx_ai_capability_version_status", "capability_id", "status"),
    )

    def to_domain(self) -> CapabilityVersionRecord:
        return CapabilityVersionRecord.model_validate({
            field: getattr(self, field) for field in CapabilityVersionRecord.model_fields
        })
