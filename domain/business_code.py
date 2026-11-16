"""Published references to reviewed business code, never Python import targets."""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain.capability import CapabilityDependency, Key, Version
from domain.task import canonical_json
from domain.task_execution import ExecutionFailure

CODE_WORKFLOW_INTERPRETER_PROFILE = "enterprise-ai.workflow.code.v1"
CODE_WORKFLOW_CHECKPOINT_PROFILE = "enterprise-ai.workflow.code.checkpoint.v1"
CODE_WORKFLOW_RESULT_KEY = "workflow-result"
MAX_CODE_FRAME_DEPTH = 8
MAX_CODE_NODES = 1000


_ACTIVITY_UNAVAILABLE_CODES = frozenset({
    "MODEL_UPSTREAM_UNAVAILABLE", "MODEL_UPSTREAM_TIMEOUT", "MODEL_RESPONSE_INVALID",
    "MODEL_RESPONSE_REFUSED", "MODEL_OUTPUT_INVALID", "MODEL_AGENT_TURN_INVALID",
    "TOOL_TIMEOUT_RESULT_UNKNOWN", "TOOL_EXECUTION_FAILED_RESULT_UNKNOWN",
    "TOOL_OUTPUT_INVALID", "TOOL_UPSTREAM_UNAVAILABLE",
})


def is_activity_unavailable(code: str) -> bool:
    """Settled operational rejections only; unknown outcomes never authorize replay."""
    return isinstance(code, str) and code in _ACTIVITY_UNAVAILABLE_CODES


class CodeReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Key
    version: Version
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodeActivity(BaseModel):
    """A bounded activity declaration, not a static node or control-flow edge."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    key: Key
    kind: Literal["step", "agent", "tool", "llm", "workflow"]
    capability: CapabilityDependency | None = None
    prompt: CapabilityDependency | None = None
    required: bool = Field(default=True, strict=True)
    max_calls: int = Field(default=1, strict=True, ge=1, le=1000)
    timeout_seconds: float = Field(default=60, strict=True, gt=0, le=86400)
    on_unavailable: Literal["fail", "continue"] = "fail"

    @model_validator(mode="after")
    def explicit_resources(self):
        if self.key == CODE_WORKFLOW_RESULT_KEY:
            raise ValueError("Reserved code Workflow result activity")
        if (self.kind == "step") != (self.capability is None):
            raise ValueError("Steps use installed code; external activities require pinned resources")
        if self.prompt is not None and self.kind != "llm":
            raise ValueError("Only LLM activities accept a pinned Prompt")
        return self


class CodeWorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition_version: Literal["workflow.code.v1"]
    implementation: CodeReference
    activities: tuple[CodeActivity, ...] = Field(min_length=1, max_length=99)
    max_parallelism: int = Field(default=4, strict=True, ge=1, le=16)

    @model_validator(mode="after")
    def unique_activities(self):
        if len({activity.key for activity in self.activities}) != len(self.activities):
            raise ValueError("Code Workflow activities must be unique")
        return self


@dataclass(frozen=True, init=False)
class ActivityCall:
    activity: str
    key: str
    _values_json: str = field(repr=False)

    def __init__(self, activity: str, values: dict, *, key: str = "once"):
        # Validate identifiers with the same contract as published declarations.
        validated = CodeActivity(key=activity, kind="step")
        if (not isinstance(key, str) or not 1 <= len(key) <= 200 or
                any(unicodedata.category(char) == "Cc" for char in key)):
            raise ValueError("Invalid activity invocation key")
        key.encode("utf-8")
        if not isinstance(values, dict):
            raise ValueError("Activity input must be a JSON object")
        object.__setattr__(self, "activity", validated.key)
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "_values_json", canonical_json(values))

    def values(self) -> dict:
        return json.loads(self._values_json)


@dataclass(frozen=True, init=False)
class ActivityOutcome:
    status: Literal["succeeded", "unavailable", "skipped"]
    _value_json: str | None = field(repr=False)
    error_code: str | None

    def __init__(self, status: str, value: dict | None = None, error_code: str | None = None):
        if status not in {"succeeded", "unavailable", "skipped"}:
            raise ValueError("Invalid activity outcome status")
        if status == "succeeded":
            if not isinstance(value, dict) or error_code is not None:
                raise ValueError("Successful activities require an object and no error")
        elif value is not None or (status == "skipped" and error_code is not None):
            raise ValueError("Unsuccessful activities cannot carry a value")
        if status == "unavailable" and not is_activity_unavailable(error_code):
            raise ValueError("Activity unavailable requires a settled operational rejection")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "_value_json", canonical_json(value) if value is not None else None)
        object.__setattr__(self, "error_code", error_code)

    @property
    def value(self) -> dict | None:
        return json.loads(self._value_json) if self._value_json is not None else None

    def require_value(self) -> dict:
        if self.status != "succeeded":
            raise ExecutionFailure(self.error_code or "CODE_ACTIVITY_SKIPPED")
        return json.loads(self._value_json)


@dataclass(frozen=True)
class CodeNodeScope:
    workflow_key: str
    version: str
    content_hash: str
    activity_key: str | None = None
    invocation_hash: str | None = None

    def __post_init__(self):
        CapabilityDependency(key=self.workflow_key, version=self.version)
        if not isinstance(self.content_hash, str) or re.fullmatch(r"[0-9a-f]{64}", self.content_hash) is None:
            raise ValueError("Invalid workflow content hash")
        if (self.activity_key is None) != (self.invocation_hash is None):
            raise ValueError("Activity identity and invocation hash must be present together")
        if self.activity_key is not None:
            CodeActivity(key=self.activity_key, kind="step")
            if not isinstance(self.invocation_hash, str) or re.fullmatch(r"[0-9a-f]{64}", self.invocation_hash) is None:
                raise ValueError("Invalid activity invocation hash")
