"""New-platform task admission contracts, independent of legacy Run states."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, field_validator

from domain.capability import Key, Version

IdempotencyKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]


def validate_business_reference(value: str) -> str:
    if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Business references must be non-blank and contain no control characters")
    return value


BusinessReference = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(validate_business_reference)]


class TaskError(ValueError):
    pass


class TaskConflict(TaskError):
    pass


class TaskNotFound(TaskError):
    pass


class TaskValidationUnavailable(RuntimeError):
    """Bounded JSON Schema validation could not run; callers may retry safely."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


def canonical_json(value) -> str:
    """Bounded, finite JSON that can safely be persisted as PostgreSQL JSONB."""
    try:
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 64:
                raise ValueError
            if isinstance(item, str):
                if "\x00" in item:
                    raise ValueError
            elif isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError
                pending.extend((child, depth + 1) for child in item.keys())
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
            elif item is not None and type(item) not in {int, float, bool}:
                raise ValueError
        content = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(content.encode("utf-8")) > 1_000_000:
            raise ValueError
        return content
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise TaskError("Task input must be bounded finite JSON with valid PostgreSQL text") from None


class TaskSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    capability_key: Key
    version: Version
    business_id: BusinessReference | None = Field(default=None, repr=False)
    customer_id: BusinessReference | None = Field(default=None, repr=False)
    input: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("input")
    @classmethod
    def valid_input(cls, value):
        canonical_json(value)
        return value

    def fingerprint(self) -> str:
        # Explicit version: changing a default never changes a submitted request.
        return hashlib.sha256(canonical_json(self.model_dump(mode="json", exclude_none=True)).encode()).hexdigest()


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    capability_key: str
    capability_kind: str
    version: str
    content_hash: str
    business_id: str | None = Field(default=None, repr=False)
    customer_id: str | None = Field(default=None, repr=False)
    status: TaskStatus
    submitted_by: str
    create_time: datetime
    finish_time: datetime | None
    cancel_request_id: str | None = None
    cancel_requested_at: datetime | None = None
    cancel_requested_by: str | None = None
    event_sequence: int = 0
    event_history_complete: bool = True
    origin_task_id: str | None = None
