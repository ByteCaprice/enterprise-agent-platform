"""Project ownership and service identities; no company user/SSO assumptions."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from domain.capability import Key


class GovernanceError(ValueError):
    pass


class GovernanceNotFound(GovernanceError):
    pass


class GovernanceConflict(GovernanceError):
    pass


class AccessDenied(GovernanceError):
    pass


class CapabilityAction(StrEnum):
    READ = "capability:read"
    WRITE = "capability:write"
    PUBLISH = "capability:publish"
    RENDER = "prompt:render"
    TASK_SUBMIT = "task:submit"
    TASK_READ = "task:read"
    TASK_CANCEL = "task:cancel"
    MODEL_INVOKE = "model:invoke"
    TOOL_INVOKE = "tool:invoke"
    AGENT_INVOKE = "agent:invoke"
    SKILL_USE = "skill:use"
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    QUALITY_RUN = "quality:run"
    QUALITY_REVIEW = "quality:review"
    FEEDBACK_READ = "feedback:read"
    FEEDBACK_WRITE = "feedback:write"


class GovernanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def valid_text(cls, value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("Invalid text") from None
            if "\x00" in value:
                raise ValueError("Invalid text")
        return value


class ProjectCreate(GovernanceInput):
    key: Key
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


class ServiceIdentityCreate(GovernanceInput):
    key: Key
    name: str = Field(min_length=1, max_length=200)


class EnabledUpdate(GovernanceInput):
    enabled: bool = Field(strict=True)


class AccessKeyCreate(GovernanceInput):
    name: str = Field(min_length=1, max_length=200)
    expires_at: AwareDatetime | None = None


class ResourceGrantCreate(GovernanceInput):
    action: CapabilityAction
    resource_key: Key | Literal["*"]
