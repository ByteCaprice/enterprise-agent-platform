"""Declarative deterministic workflows, without Python expressions or dynamic imports."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from domain.capability import CapabilityDependency, Key, Version


class WorkflowBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: Literal["input", "node", "literal"]
    node: Key | None = None
    pointer: str = Field(default="", max_length=1024)
    value: JsonValue = None
    required: bool = Field(default=True, strict=True)
    default: JsonValue = None

    @field_validator("pointer")
    @classmethod
    def json_pointer(cls, value):
        if (value and not value.startswith("/")) or re.search(r"~(?![01])", value):
            raise ValueError("Binding pointer must be an RFC 6901 JSON pointer")
        return value

    @model_validator(mode="after")
    def valid_source(self) -> Self:
        if self.source == "literal":
            if self.node is not None or self.pointer or not self.required or self.default is not None:
                raise ValueError("Literal bindings only accept a value")
        elif self.value is not None:
            raise ValueError("Only literal bindings accept a value")
        if self.source == "node" and self.node is None:
            raise ValueError("Node bindings require a node key")
        if self.source != "node" and self.node is not None:
            raise ValueError("Only node bindings accept a node key")
        if self.required and self.default is not None:
            raise ValueError("Defaults require required=false")
        return self


class WorkflowRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["project", "equals", "assert"]


Bindings = Annotated[dict[str, WorkflowBinding], Field(max_length=100)]


class WorkflowNode(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    key: Key
    kind: Literal["agent", "tool", "llm", "rule"]
    capability: CapabilityDependency | None = None
    prompt: CapabilityDependency | None = None
    rule: WorkflowRule | None = None
    depends_on: list[Key] = Field(default_factory=list, max_length=100)
    input_mapping: Bindings = Field(default_factory=dict)
    when: WorkflowBinding | None = None
    timeout_seconds: float = Field(default=60, strict=True, gt=0, le=86400)
    max_attempts: int = Field(default=1, strict=True, ge=1, le=10)

    @model_validator(mode="after")
    def valid_kind(self) -> Self:
        if self.kind == "rule":
            if self.rule is None or self.capability is not None:
                raise ValueError("Rule nodes require an inline rule and cannot reference an executable capability")
        elif self.capability is None or self.rule is not None:
            raise ValueError("Agent/Tool/LLM nodes require a pinned capability and cannot have inline rules")
        if self.prompt is not None and self.kind != "llm":
            raise ValueError("Only LLM nodes accept a pinned Prompt reference")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("Duplicate workflow node dependencies")
        return self


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    nodes: list[WorkflowNode] = Field(min_length=1, max_length=100)
    output_mapping: Bindings = Field(default_factory=dict)
    max_parallel_rules: int = Field(default=1, strict=True, ge=1, le=8)

    def ordered_nodes(self) -> list[WorkflowNode]:
        nodes = {node.key: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("Workflow node keys must be unique")
        ordered: list[WorkflowNode] = []
        ancestors: dict[str, set[str]] = {}
        while len(ordered) < len(nodes):
            ready = [node for node in self.nodes if node.key not in ancestors and set(node.depends_on) <= ancestors.keys()]
            if not ready:
                raise ValueError("Workflow contains a cycle or unknown dependency")
            for node in ready:
                ancestors[node.key] = set(node.depends_on)
                for dependency in node.depends_on:
                    ancestors[node.key].update(ancestors[dependency])
                bindings = list(node.input_mapping.values()) + ([node.when] if node.when is not None else [])
                if any(binding.source == "node" and binding.node not in ancestors[node.key] for binding in bindings):
                    raise ValueError("Workflow node references must target an explicit dependency ancestor")
                ordered.append(node)
        if any(binding.source == "node" and binding.node not in nodes for binding in self.output_mapping.values()):
            raise ValueError("Workflow output references an unknown node")
        return ordered


class WorkflowNodeRecord(BaseModel):
    """Timeline metadata: no input/output payload, definition or lease credential."""

    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    task_attempt_id: str
    node_key: str
    kind: str
    status: str
    input_hash: str
    output_hash: str | None = None
    reused_from_node_id: str | None = None
    start_time: datetime
    finish_time: datetime | None = None
    error_code: str | None = None


class WorkflowPlanNodeRecord(BaseModel):
    """Static graph metadata, without bindings, literals, customer input or output."""

    model_config = ConfigDict(extra="forbid")

    key: Key
    kind: Literal["agent", "tool", "llm", "rule"]
    depends_on: list[Key]
    capability: CapabilityDependency | None = None
    prompt: CapabilityDependency | None = None
    timeout_seconds: float
    max_attempts: int
    conditional: bool = False
    rule_operation: Literal["project", "equals", "assert"] | None = None


class WorkflowPlanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_key: Key
    capability_key: Key
    version: Version
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_profile: Literal["enterprise-ai.workflow.plan.v1"] = "enterprise-ai.workflow.plan.v1"
    required_adapters: list[Literal["agent", "tool", "llm"]]
    max_parallel_rules: int
    nodes: list[WorkflowPlanNodeRecord]
    requires_runtime_validation: Literal[True] = True


@dataclass(frozen=True)
class WorkflowCheckpoint:
    record: WorkflowNodeRecord
    output: dict | None = field(default=None, repr=False)
    created: bool = False
