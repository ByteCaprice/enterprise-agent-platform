"""Operator-reviewed MCP deployment catalog; never supplied by Task arguments."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.capability import Key
from domain.platform_model import normalize_provider_url
from domain.schema_validation import JSONValidationError, ensure_self_contained_schema
from domain.task import canonical_json

RemoteToolName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]


class MCPConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_key: Key
    key: Key
    endpoint: str = Field(max_length=2048)
    credential_ref: Key
    transport: Literal["streamable_http_json"]
    protocol_version: Literal["2025-06-18"] = "2025-06-18"

    @field_validator("endpoint")
    @classmethod
    def public_https_endpoint(cls, value):
        normalized = normalize_provider_url(value)
        # Unlike a model base URL, this is the exact MCP endpoint, including a trailing slash.
        return normalized + "/" if value.endswith("/") else normalized


class MCPToolPin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_key: Key
    connection_key: Key
    handler_key: Key
    remote_tool: RemoteToolName
    operation_type: Literal["read_only"]
    input_schema: dict
    output_schema: dict

    @field_validator("input_schema", "output_schema")
    @classmethod
    def bounded_local_object_schema(cls, value):
        canonical_json(value)
        if value.get("type") != "object":
            raise ValueError("MCP pins require an explicit object schema")
        try:
            ensure_self_contained_schema(value)
        except JSONValidationError:
            raise ValueError("MCP schemas require bounded local references") from None
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError:
            raise ValueError("Invalid MCP JSON Schema") from None
        return value

    def handler_version(self, connection: MCPConnection) -> str:
        if (self.project_key, self.connection_key) != (connection.project_key, connection.key):
            raise ValueError("MCP pin does not belong to this connection")
        snapshot = {"profile": "enterprise-ai-mcp-json-v1", "connection": connection.model_dump(mode="json"),
                    "tool": self.model_dump(mode="json")}
        return hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()


class MCPCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connections: list[MCPConnection] = Field(default_factory=list, max_length=100)
    tools: list[MCPToolPin] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def explicit_unique_references(self):
        connections = {(item.project_key, item.key): item for item in self.connections}
        if len(connections) != len(self.connections):
            raise ValueError("Duplicate MCP connection")
        handlers = set()
        for tool in self.tools:
            connection = connections.get((tool.project_key, tool.connection_key))
            if connection is None:
                raise ValueError("MCP tool must reference its project's installed connection")
            handler = (tool.handler_key, tool.handler_version(connection))
            if handler in handlers:
                raise ValueError("Duplicate MCP handler pin")
            handlers.add(handler)
        canonical_json(self.model_dump(mode="json"))
        return self
