"""Transport-facing response contracts shared by runtime and offline OpenAPI."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from domain.governance import CapabilityAction


class APIErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    request_id: str | None


class APIErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: APIErrorDetail


class SessionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    is_admin: bool
    project_key: str | None
    identity_id: str | None


class PromptRenderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(repr=False)
    version: str
    content_hash: str
    rendered_hash: str


class _PublicRecord(BaseModel):
    # Storage may gain internal columns; only declared fields belong to the API.
    model_config = ConfigDict(extra="ignore")

    created_by: str
    create_time: datetime


class ProjectResponse(_PublicRecord):
    key: str
    name: str
    description: str
    enabled: bool


class ServiceIdentityResponse(_PublicRecord):
    id: str
    project_key: str
    key: str
    name: str
    enabled: bool


class ServiceKeyResponse(_PublicRecord):
    """Key metadata only: neither bearer token nor stored digest."""

    id: str
    project_key: str
    identity_id: str
    name: str
    revoked: bool
    expires_at: datetime | None


class IssuedServiceKeyResponse(ServiceKeyResponse):
    token: str = Field(repr=False)


class ResourceGrantResponse(_PublicRecord):
    id: str
    project_key: str
    identity_id: str
    action: CapabilityAction
    resource_key: str
