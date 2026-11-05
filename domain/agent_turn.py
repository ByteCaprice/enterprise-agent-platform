"""Platform-normalized Agent turns and conversations; no Pi or provider SDK types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from domain.platform_agent import ToolAlias
from domain.platform_model import ModelSecret, ModelUsage
from domain.task import canonical_json

ProviderCallId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")]


def strict_json(value: str | bytes):
    def unique_pairs(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = child
        return result

    def reject_constant(_value):
        raise ValueError("Non-finite JSON")

    return json.loads(value, object_pairs_hook=unique_pairs, parse_constant=reject_constant)


class AgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ProviderCallId
    alias: ToolAlias
    arguments: dict[str, JsonValue] = Field(repr=False)

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value):
        canonical_json(value)
        return value


class AgentModelTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["final", "tools"]
    text: str = Field(default="", max_length=262144, repr=False)
    tool_calls: tuple[AgentToolCall, ...] = Field(default=(), max_length=512, repr=False)

    @model_validator(mode="after")
    def complete_decision(self):
        if (self.kind == "tools") != bool(self.tool_calls) or (self.kind == "final" and not self.text.strip()):
            raise ValueError("A turn is a complete final answer or a nonempty tool batch")
        if len({call.id for call in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("Duplicate provider tool-call ID")
        canonical_json(self.model_dump(mode="json"))
        return self


class AgentTextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user"]
    content: str = Field(min_length=1, max_length=262144, repr=False)


class AgentAssistantMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["assistant"] = "assistant"
    content: str = Field(default="", max_length=262144, repr=False)
    tool_calls: tuple[AgentToolCall, ...] = Field(min_length=1, max_length=512, repr=False)


class AgentToolMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["tool"] = "tool"
    tool_call_id: ProviderCallId
    content: str = Field(min_length=1, max_length=1_000_000, repr=False)


AgentMessage = Annotated[AgentTextMessage | AgentAssistantMessage | AgentToolMessage, Field(discriminator="role")]


class AgentConversation(BaseModel):
    """Request-ready, bounded transcript; partial tool batches cannot request a model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[AgentMessage, ...] = Field(min_length=2, max_length=1024, repr=False)

    @model_validator(mode="after")
    def canonical_order(self):
        if self.messages[0].role != "system" or self.messages[1].role != "user":
            raise ValueError("Agent conversation starts with trusted instructions and Task input")
        pending = []
        for message in self.messages[2:]:
            if pending:
                if not isinstance(message, AgentToolMessage) or message.tool_call_id != pending.pop(0):
                    raise ValueError("Tool results must match the accepted batch in source order")
            elif isinstance(message, AgentAssistantMessage):
                ids = [call.id for call in message.tool_calls]
                if len(set(ids)) != len(ids):
                    raise ValueError("Duplicate tool-call ID")
                pending = ids
            else:
                raise ValueError("Unsolicited conversation message")
        if pending:
            raise ValueError("Tool batch is incomplete")
        canonical_json(self.model_dump(mode="json"))
        return self


class AgentToolDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: ToolAlias
    description: str = Field(max_length=4000)
    input_schema: dict


@dataclass(frozen=True)
class AgentTurnRequest:
    base_url: str
    model: str
    conversation: AgentConversation = field(repr=False)
    tools: tuple[AgentToolDescriptor, ...] = field(repr=False)
    secret: ModelSecret = field(repr=False)
    timeout_seconds: float
    max_tokens: int
    temperature: float | None = None
    request_profile: Literal["openai_chat", "openrouter_non_reasoning"] = "openai_chat"


class AgentTurnResponse(BaseModel):
    """A rejected complete response can still carry trustworthy Usage metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: AgentModelTurn | None = Field(default=None, repr=False)
    error_code: str | None = Field(default=None, pattern=r"^MODEL_[A-Z0-9_]{1,90}$")
    actual_model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,199}$")
    usage: ModelUsage = Field(default_factory=ModelUsage)
    http_status: int = Field(default=200, strict=True, ge=100, le=599)
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,200}$")

    @model_validator(mode="after")
    def exclusive_outcome(self):
        if (self.turn is None) == (self.error_code is None) or (self.turn is not None and self.actual_model is None):
            raise ValueError("Response requires an accepted turn/model or a safe error")
        return self


class AgentTurnTransport(Protocol):
    async def generate_turn(self, request: AgentTurnRequest) -> AgentTurnResponse: ...


class RuntimeToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dispatch_id: str
    alias: ToolAlias
    arguments: dict[str, JsonValue] = Field(repr=False)


class RuntimeTurn(BaseModel):
    """Pi-facing value, never provider IDs, credentials, owner IDs or lease tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["final", "tools"]
    text: str = Field(repr=False)
    tool_calls: tuple[RuntimeToolCall, ...] = Field(default=(), repr=False)
