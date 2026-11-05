"""New-platform project, service credential and resource grant storage."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, ForeignKeyConstraint, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from infra.store.model.base import PlatformBase as Base
from infra.store.model.base import UTCDateTime


class AiProject(Base):
    __tablename__ = "ai_project"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class AiServiceIdentity(Base):
    __tablename__ = "ai_service_identity"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(ForeignKey("ai_project.key"), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_key", "key", name="uk_ai_service_identity_key"),
        UniqueConstraint("id", "project_key", name="uk_ai_service_identity_project"),
    )


class AiServiceAccessKey(Base):
    __tablename__ = "ai_service_access_key"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False)
    identity_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["identity_id", "project_key"], ["ai_service_identity.id", "ai_service_identity.project_key"]),
        CheckConstraint("digest ~ '^sha256:[0-9a-f]{64}$'", name="ck_ai_service_key_digest"),
    )


class AiResourceGrant(Base):
    __tablename__ = "ai_resource_grant"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, nullable=False)
    identity_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["identity_id", "project_key"], ["ai_service_identity.id", "ai_service_identity.project_key"]),
        UniqueConstraint("identity_id", "action", "resource_key", name="uk_ai_resource_grant"),
        CheckConstraint("action IN ('capability:read','capability:write','capability:publish','prompt:render',"
                        "'task:submit','task:read','task:cancel','model:invoke','tool:invoke','agent:invoke',"
                        "'memory:read','memory:write','quality:run','quality:review','skill:use',"
                        "'feedback:read','feedback:write')",
                        name="ck_ai_resource_grant_action"),
    )
