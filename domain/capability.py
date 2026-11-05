"""Versioned platform capabilities; Prompt/Skill artifacts are not executable tasks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")]
Version = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")]
ModelName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,199}$")]


class CapabilityKind(StrEnum):
    AGENT = "agent"
    WORKFLOW = "workflow"
    TOOL = "tool"
    SKILL = "skill"
    PROMPT = "prompt"
    MODEL_POLICY = "model_policy"


class CapabilityError(ValueError):
    pass


class CapabilityConflict(CapabilityError):
    pass


class CapabilityNotFound(CapabilityError):
    pass


class CapabilityValidationUnavailable(RuntimeError):
    """Bounded JSON Schema validation could not run; callers may retry safely."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CapabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Key
    kind: CapabilityKind
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


class CapabilityRecord(CapabilitySpec):
    id: str
    project_key: Key
    default_version: Version | None
    created_by: str
    create_time: datetime


class CapabilityDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Key
    version: Version


class CapabilityVersionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    version: Version
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    definition: dict[str, Any]
    dependencies: list[CapabilityDependency] = Field(default_factory=list, max_length=100)

    def fingerprint(self) -> str:
        """Canonical published content, excluding mutable record and publication metadata."""
        try:
            data = self.model_dump(mode="json", include=set(CapabilityVersionDraft.model_fields))
            content = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            encoded = content.encode("utf-8")
            pending: list[Any] = [data]
            while pending:
                value = pending.pop()
                if isinstance(value, str) and "\x00" in value:
                    raise ValueError
                if isinstance(value, dict):
                    pending.extend(value.keys())
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise CapabilityError("Capability content must be finite JSON with valid PostgreSQL text") from None
        return hashlib.sha256(encoded).hexdigest()


class CapabilityVersionRecord(CapabilityVersionDraft):
    id: str
    capability_id: str
    status: str
    content_hash: str
    created_by: str
    create_time: datetime
    published_by: str | None = None
    publish_time: datetime | None = None

    def verify_integrity(self) -> None:
        try:
            actual = self.fingerprint()
        except CapabilityError:
            raise CapabilityConflict("Capability artifact content is invalid") from None
        if actual != self.content_hash:
            raise CapabilityConflict("Capability artifact content hash does not match")

    def require_published(self, *, version: str | None = None, content_hash: str | None = None,
                          capability_id: str | None = None) -> None:
        """Check a pinned artifact, not caller permission or adapter availability."""
        if self.status != "published":
            raise CapabilityConflict("Only published capability versions can be used")
        if ((version is not None and self.version != version) or
                (content_hash is not None and self.content_hash != content_hash) or
                (capability_id is not None and self.capability_id != capability_id)):
            raise CapabilityConflict("Capability artifact does not match its pinned identity")
        self.verify_integrity()


class CapabilityReader(Protocol):
    """Read-only catalog port; no publication commands, storage sessions or defaults."""

    def get(self, project_key: str, key: str) -> Mapping[str, Any]: ...

    def get_version(self, project_key: str, key: str, version: str) -> CapabilityVersionRecord: ...


class PromptDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1, max_length=100_000)


class SkillDefinition(BaseModel):
    """Versioned Agent guidance only; it cannot install code or grant tools."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instructions: str = Field(min_length=1, max_length=32_000, repr=False)


class PromptRenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    variables: dict[str, Any] = Field(default_factory=dict)


class ModelPolicyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    interaction: Literal["completion"] = "completion"
    provider_key: Key
    model: ModelName
    fallback_models: list[ModelName] = Field(default_factory=list, max_length=10)
    timeout_seconds: float = Field(default=60, strict=True, gt=0, le=3600)
    max_tokens: int = Field(default=4096, strict=True, gt=0, le=1_000_000)
    response_format: Literal["text", "json_object"] = "json_object"
    temperature: float | None = Field(default=None, strict=True, ge=0, le=2)
    credential_ref: Key | None = None

    @model_validator(mode="after")
    def distinct_candidates(self):
        candidates = [self.model, *self.fallback_models]
        if len(set(candidates)) != len(candidates):
            raise ValueError("Model candidates must be unique")
        return self


class AgentTurnPolicyDefinition(BaseModel):
    """Explicit single-candidate tool-calling policy, separate from completion mode."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    interaction: Literal["agent_turn"]
    request_profile: Literal["openai_chat", "openrouter_non_reasoning"] = "openai_chat"
    provider_key: Key
    model: ModelName
    timeout_seconds: float = Field(default=60, strict=True, gt=0, le=3600)
    max_tokens: int = Field(default=4096, strict=True, gt=0, le=1_000_000)
    temperature: float | None = Field(default=None, strict=True, ge=0, le=2)
    credential_ref: Key | None = None
