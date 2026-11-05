"""Technical per-process HTTP bounds, not Task deadlines or tenant quotas."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HTTPIngressPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    max_body_bytes: int = Field(default=2_000_000, ge=1, le=64_000_000)
    max_header_bytes: int = Field(default=32768, ge=1, le=1_048_576)
    max_target_bytes: int = Field(default=8192, ge=1, le=65536)
    body_read_timeout_seconds: float = Field(default=15, gt=0, le=300)
    response_send_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_inflight_requests: int = Field(default=64, ge=1, le=10000)
    reserved_probe_requests: int = Field(default=2, ge=1, le=16)

    @field_validator("max_body_bytes", "max_header_bytes", "max_target_bytes", "body_read_timeout_seconds",
                     "response_send_timeout_seconds", "max_inflight_requests", "reserved_probe_requests", mode="before")
    @classmethod
    def not_boolean(cls, value):
        # Environment overrides are strings, but booleans must not become 0/1.
        if isinstance(value, bool):
            raise ValueError("HTTP resource bounds must be numeric")
        return value
