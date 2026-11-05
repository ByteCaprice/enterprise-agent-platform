"""Platform operations, quality, callback delivery and memory tables."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from infra.store.model.base import JSON_VARIANT, UTCDateTime
from infra.store.model.base import PlatformBase as Base


class AiProjectLimits(Base):
    __tablename__ = "ai_project_limits"
    project_key: Mapped[str] = mapped_column(ForeignKey("ai_project.key"), primary_key=True)
    max_queued: Mapped[int | None] = mapped_column(Integer)
    max_running: Mapped[int | None] = mapped_column(Integer)
    active_budget_key: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (CheckConstraint("(max_queued IS NULL OR max_queued>=0) AND (max_running IS NULL OR max_running>=0)", name="ck_ai_project_limits"),)


class AiGlobalConcurrency(Base):
    __tablename__ = "ai_global_concurrency"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    max_running: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("key='global' AND (max_running IS NULL OR max_running BETWEEN 0 AND 100000)", name="ck_ai_global_concurrency"),)


class AiCapabilityConcurrency(Base):
    __tablename__ = "ai_capability_concurrency"
    capability_id: Mapped[str] = mapped_column(ForeignKey("ai_capability.id"), primary_key=True)
    max_running: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("max_running IS NULL OR max_running BETWEEN 0 AND 100000", name="ck_ai_capability_concurrency"),)


class AiBudgetCycle(Base):
    __tablename__ = "ai_budget_cycle"
    project_key: Mapped[str] = mapped_column(ForeignKey("ai_project.key"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    currency: Mapped[str] = mapped_column(Text)
    limit_microunits: Mapped[int] = mapped_column(BigInteger)
    reserve_per_call_microunits: Mapped[int] = mapped_column(BigInteger)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)
    rates: Mapped[list] = mapped_column(JSON_VARIANT)
    __table_args__ = (CheckConstraint("limit_microunits>0 AND reserve_per_call_microunits>0 AND ends_at>starts_at AND currency ~ '^[A-Z]{3}$'", name="ck_ai_budget_cycle"),)


class AiBudgetCharge(Base):
    __tablename__ = "ai_budget_charge"
    call_id: Mapped[str] = mapped_column(ForeignKey("ai_platform_model_call.id"), primary_key=True)
    project_key: Mapped[str] = mapped_column(Text)
    budget_key: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str] = mapped_column(ForeignKey("ai_task.id"))
    reserved_microunits: Mapped[int] = mapped_column(BigInteger)
    settled_microunits: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(Text)
    reconciliation_ref: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index("ix_ai_budget_charge_cycle", "project_key", "budget_key", "call_id"),
        Index("ix_ai_budget_charge_task", "task_id", "state"),
        ForeignKeyConstraint(["project_key", "budget_key"], ["ai_budget_cycle.project_key", "ai_budget_cycle.key"]),
        CheckConstraint("reserved_microunits>0 AND (settled_microunits IS NULL OR settled_microunits>=0) AND "
                        "((state IN ('reserved','unknown') AND settled_microunits IS NULL) OR (state IN ('known','reconciled') AND settled_microunits IS NOT NULL))", name="ck_ai_budget_charge"),)


class AiCallbackEndpoint(Base):
    __tablename__ = "ai_callback_endpoint"
    project_key: Mapped[str] = mapped_column(ForeignKey("ai_project.key"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    credential_ref: Mapped[str] = mapped_column(Text)
    max_attempts: Mapped[int] = mapped_column(Integer)
    retry_delay_seconds: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = (CheckConstraint("max_attempts BETWEEN 1 AND 20 AND retry_delay_seconds BETWEEN 1 AND 86400 AND timeout_seconds BETWEEN 1 AND 30", name="ck_ai_callback_endpoint"),)


class AiCallbackDelivery(Base):
    __tablename__ = "ai_callback_delivery"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text)
    endpoint_key: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str] = mapped_column(Text)
    event_sequence: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON_VARIANT)
    state: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime)
    lease_token: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index("ix_ai_callback_delivery_due", "state", "next_attempt_at", "id"),
        Index("ix_ai_callback_delivery_expiry", "state", "lease_expires_at"),
        Index("ix_ai_callback_delivery_project", "project_key", "id"),
        ForeignKeyConstraint(["project_key", "endpoint_key"], ["ai_callback_endpoint.project_key", "ai_callback_endpoint.key"]),
        ForeignKeyConstraint(["task_id", "event_sequence"], ["ai_task_event.task_id", "ai_task_event.sequence"]),
        UniqueConstraint("task_id", "event_sequence", "endpoint_key"),
        CheckConstraint("state IN ('pending','leased','delivered','dead') AND attempts>=0 AND "
                        "((state='leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
                        "(state!='leased' AND lease_token IS NULL AND lease_expires_at IS NULL))", name="ck_ai_callback_delivery"),)


class AiQualityDataset(Base):
    __tablename__ = "ai_quality_dataset"
    project_key: Mapped[str] = mapped_column(ForeignKey("ai_project.key"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    owner: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    cases: Mapped[list] = mapped_column(JSON_VARIANT)


class AiQualityRun(Base):
    __tablename__ = "ai_quality_run"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text)
    dataset_key: Mapped[str] = mapped_column(Text)
    dataset_version: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(Text)
    capability_key: Mapped[str] = mapped_column(Text)
    capability_version: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)
    task_ids: Mapped[list] = mapped_column(JSON_VARIANT)
    scores: Mapped[list] = mapped_column(JSON_VARIANT)
    state: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        ForeignKeyConstraint(["project_key", "dataset_key", "dataset_version"], ["ai_quality_dataset.project_key", "ai_quality_dataset.key", "ai_quality_dataset.version"]),
        Index("ix_ai_quality_run_owner", "project_key", "owner", "id"),
    )


class AiTaskReview(Base):
    __tablename__ = "ai_task_review"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(Text)
    comment: Mapped[str] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (ForeignKeyConstraint(["task_id", "project_key"], ["ai_task.id", "ai_task.project_key"]),
                       Index("ix_ai_task_review_task", "project_key", "task_id", "id"),
                      CheckConstraint("verdict IN ('approved','rejected','needs_review')", name="ck_ai_task_review"))


class AiTaskFeedback(Base):
    __tablename__ = "ai_task_feedback"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str] = mapped_column(Text)
    submitter: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    request_digest: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)
    adoption: Mapped[str] = mapped_column(Text)
    final_result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    reason: Mapped[str] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (
        ForeignKeyConstraint(["task_id", "project_key"], ["ai_task.id", "ai_task.project_key"]),
        UniqueConstraint("task_id", "submitter", "revision", name="uk_ai_task_feedback_revision"),
        UniqueConstraint("task_id", "submitter", "request_digest", name="uk_ai_task_feedback_request"),
        CheckConstraint("revision > 0 AND request_digest ~ '^[0-9a-f]{64}$' AND fingerprint ~ '^[0-9a-f]{64}$'", name="ck_ai_task_feedback_identity"),
        CheckConstraint("(adoption='modified' AND final_result IS NOT NULL AND jsonb_typeof(final_result)='object') OR "
                        "(adoption IN ('adopted','not_adopted') AND final_result IS NULL)", name="ck_ai_task_feedback_outcome"),
        Index("ix_ai_task_feedback_task", "project_key", "task_id", "submitter", "revision"),
    )


class AiMemoryFact(Base):
    __tablename__ = "ai_memory_fact"
    project_key: Mapped[str] = mapped_column(ForeignKey("ai_project.key"), primary_key=True)
    owner: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_key: Mapped[str] = mapped_column(Text, primary_key=True)
    fact_key: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    capability_key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[object | None] = mapped_column(JSON_VARIANT)
    state: Mapped[str] = mapped_column(Text)
    source_task_id: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    create_time: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (ForeignKeyConstraint(["source_task_id", "project_key"], ["ai_task.id", "ai_task.project_key"]),
                       Index("ix_ai_memory_fact_current", "project_key", "owner", "capability_key", "subject_key", "fact_key", "revision"),
                      CheckConstraint("revision>0 AND state IN ('active','deleted')", name="ck_ai_memory_fact"))


class AiOIDCProvider(Base):
    __tablename__ = "ai_oidc_provider"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    issuer: Mapped[str] = mapped_column(Text, unique=True)
    audience: Mapped[str] = mapped_column(Text)
    jwks: Mapped[dict] = mapped_column(JSON_VARIANT)
    enabled: Mapped[bool] = mapped_column(Boolean)


class AiOIDCBinding(Base):
    __tablename__ = "ai_oidc_binding"
    provider_key: Mapped[str] = mapped_column(ForeignKey("ai_oidc_provider.key"), primary_key=True)
    subject: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text)
    identity_id: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = (ForeignKeyConstraint(["identity_id", "project_key"], ["ai_service_identity.id", "ai_service_identity.project_key"]),)
