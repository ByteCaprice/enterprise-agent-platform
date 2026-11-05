"""Task-scoped delivery observations, not delivery leases or execution authority."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from domain.execution_query import MetadataPage


class CallbackDeliveryView(BaseModel):
    """Allowlisted metadata: no payload, endpoint URL, secret reference or lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    project_key: str
    task_id: str
    endpoint_key: str
    event_sequence: int
    state: Literal["pending", "leased", "delivered", "dead"]
    attempts: int
    next_attempt_at: datetime
    http_status: int | None
    error_code: str | None


class CallbackDeliveryPage(MetadataPage[CallbackDeliveryView]):
    pass
