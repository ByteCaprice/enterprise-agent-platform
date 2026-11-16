"""New-platform model contracts, independent of legacy Run/model configuration."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.capability import Key, ModelName
from domain.outbound_operation import ModelUsage, OutboundEvidence
from domain.platform_agent import CallOwnership
from domain.task import canonical_json


class ModelConfigurationError(ValueError):
    pass


class ModelProviderNotFound(ModelConfigurationError):
    pass


class ModelProviderConflict(ModelConfigurationError):
    pass


class ModelCallConflict(ModelConfigurationError):
    pass


def is_public_model_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_global and not any((address.is_reserved, address.is_multicast,
                                              address.is_loopback, address.is_link_local,
                                              address.is_unspecified, address.is_private))
    except ValueError:
        return False


def normalize_provider_url(value: str) -> str:
    try:
        if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or
                parsed.password is not None or parsed.query or parsed.fragment or parsed.port not in {None, 443}):
            raise ValueError
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or re.fullmatch(r"[a-z0-9.-]+", host) is None:
                raise ValueError from None
        else:
            if not is_public_model_address(str(address)):
                raise ValueError
        netloc = f"[{host}]" if ":" in host else host
        path = parsed.path.rstrip("/")
        if any(part in {".", ".."} for part in path.split("/")):
            raise ValueError
        return urlunsplit(("https", netloc, path, "", ""))
    except (ValueError, UnicodeError):
        raise ValueError("Provider requires a public HTTPS base URL without credentials, query, fragment or non-443 port") from None


class ModelProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    key: Key
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(max_length=2048)
    credential_ref: Key
    models: list[ModelName] = Field(min_length=1, max_length=100)
    max_tokens: int = Field(default=32768, strict=True, ge=1, le=1_000_000)

    @field_validator("base_url")
    @classmethod
    def safe_base_url(cls, value):
        return normalize_provider_url(value)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        canonical_json(value)
        return value

    @field_validator("models")
    @classmethod
    def unique_models(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Provider catalog must not contain duplicate model names")
        return value


class ModelProviderRecord(ModelProviderCreate):
    project_key: str
    enabled: bool
    created_by: str
    create_time: datetime


class ModelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=262144)

    @field_validator("content")
    @classmethod
    def finite_text(cls, value):
        canonical_json(value)
        return value


class ModelMessages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ModelMessage] = Field(min_length=1, max_length=100)


@dataclass(frozen=True)
class ModelSecret:
    reference: str
    value: str = field(repr=False)


class CredentialResolver(Protocol):
    def resolve(self, project_key: str, reference: str) -> ModelSecret: ...


@dataclass(frozen=True)
class ModelTransportRequest:
    base_url: str
    model: str
    messages: tuple[ModelMessage, ...] = field(repr=False)
    secret: ModelSecret = field(repr=False)
    timeout_seconds: float
    max_tokens: int
    response_format: Literal["text", "json_object"] = "json_object"
    temperature: float | None = None


@dataclass(frozen=True)
class ModelTransportResponse:
    content: str = field(repr=False)
    actual_model: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    http_status: int = 200
    request_id: str | None = None

    @property
    def evidence(self) -> OutboundEvidence:
        return OutboundEvidence(dispatch="dispatched", response="received", actual_model=self.actual_model,
                                usage=self.usage, http_status=self.http_status, request_id=self.request_id)


class ModelTransportFailure(RuntimeError):
    """Safe transport classification; never include provider bodies or credentials."""

    def __init__(self, code: str, *, evidence: OutboundEvidence | None = None,
                 http_status: int | None = None, retryable: bool = False):
        if re.fullmatch(r"MODEL_[A-Z0-9_]{1,90}", code) is None:
            raise ValueError("Invalid model transport error code")
        self.code = code
        # A supplied status records headers only; legacy retry hints cannot grant replay.
        self._evidence = OutboundEvidence.model_validate(evidence) if evidence is not None else OutboundEvidence(
            dispatch="dispatched" if http_status is not None else "unknown",
            response="headers_received" if http_status is not None else "not_received",
            http_status=http_status,
        )
        super().__init__(code)

    @property
    def evidence(self) -> OutboundEvidence:
        return self._evidence

    @property
    def http_status(self) -> int | None:
        return self.evidence.http_status

    @property
    def retryable(self) -> bool:
        return self.code == "MODEL_UPSTREAM_UNAVAILABLE" and self.evidence.retry_permission == "safe_response"


class ModelTransport(Protocol):
    async def complete(self, request: ModelTransportRequest) -> ModelTransportResponse: ...


class PlatformModelCallRecord(CallOwnership):
    """Public call accounting; excludes prompts, results, secrets and lease tokens."""

    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    task_id: str
    task_attempt_id: str
    policy_key: str
    policy_version: str
    policy_hash: str
    provider_key: str
    requested_model: str
    actual_model: str | None = None
    prompt_key: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    status: str
    input_hash: str
    output_hash: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    http_status: int | None = None
    provider_request_id: str | None = None
    error_code: str | None = None
    start_time: datetime
    finish_time: datetime | None = None
