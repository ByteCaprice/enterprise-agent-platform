"""Technical runtime settings and explicit operator attestations, not business policy defaults."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.http_ingress import HTTPIngressPolicy


class PlatformRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: Literal["trial", "production"] = "trial"
    policy_file: str | None = None
    worker_stale_seconds: int = Field(default=45, ge=15, le=3600)
    queue_warning_seconds: int = Field(default=300, ge=1, le=86400)
    http_ingress: HTTPIngressPolicy = Field(default_factory=HTTPIngressPolicy)


class DeploymentPolicy(BaseModel):
    """References to reviewed policies. Validation cannot verify their external implementation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    owner: str = Field(min_length=1, max_length=200)
    identity_policy_ref: str = Field(min_length=1, max_length=500)
    financial_policy_ref: str = Field(min_length=1, max_length=500)
    retention_policy_ref: str = Field(min_length=1, max_length=500)
    backup_restore_ref: str = Field(min_length=1, max_length=500)
    incident_runbook_ref: str = Field(min_length=1, max_length=500)
    edge_security_ref: str = Field(min_length=1, max_length=500)
    # Current application rate limits are process-local. A production edge must
    # enforce global limits, TLS and request-size limits before this service.
    shared_edge_limits_confirmed: Literal[True]
