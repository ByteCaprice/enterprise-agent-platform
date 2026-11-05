"""Structured platform audit events; TaskEvent owns the execution timeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from domain.ids import utc_now


class LogEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    trace_id: str
    component: str
    event_type: str
    level: str = "INFO"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    create_time: datetime = Field(default_factory=utc_now)
