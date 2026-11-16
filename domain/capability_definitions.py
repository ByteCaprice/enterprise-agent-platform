"""Closed, versioned interpretation of capability data, shared by all planes.

This is not an extensible plugin loader or a second artifact registry. Parsing,
semantic admissibility and dependency roles must mean the same thing at authoring,
plan inspection, execution and checkpoint recovery. Installation and permission
remain separate decisions, made against the live deployment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from string import Template
from typing import Literal

from domain.business_code import CODE_WORKFLOW_INTERPRETER_PROFILE, CodeWorkflowDefinition
from domain.capability import (
    AgentTurnPolicyDefinition,
    CapabilityError,
    CapabilityKind,
    CapabilityVersionDraft,
    ModelPolicyDefinition,
    PromptDefinition,
    SkillDefinition,
)
from domain.platform_agent import AgentDefinition
from domain.platform_tool import ToolDefinition
from domain.task import canonical_json
from domain.workflow import WorkflowDefinition

WORKFLOW_INTERPRETER_PROFILE = "enterprise-ai.workflow.interpreter.v1"
WORKFLOW_CHECKPOINT_PROFILE = "enterprise-ai.workflow.checkpoint.v2"

Definition = AgentDefinition | WorkflowDefinition | CodeWorkflowDefinition | ToolDefinition | PromptDefinition | SkillDefinition | ModelPolicyDefinition | AgentTurnPolicyDefinition


@dataclass(frozen=True)
class DefinitionReference:
    key: str
    version: str
    kind: CapabilityKind
    role: Literal["resource", "instruction", "completion", "agent_turn", "code_workflow"] = "resource"


@dataclass(frozen=True)
class DefinitionInterpretation:
    kind: CapabilityKind
    profile: str
    snapshot: str = field(repr=False)
    references: tuple[DefinitionReference, ...] = ()

    def materialize(self) -> Definition:
        """No caller receives a mutable view into a validated interpretation."""
        return _parse(self.kind, self.snapshot)


def validate_prompt_template(text: str, properties: dict) -> None:
    template = Template(text)
    if not template.is_valid():
        raise CapabilityError("Prompt uses invalid template syntax; use ${variable}")
    if set(template.get_identifiers()) - set(properties):
        raise CapabilityError("Prompt variables must be declared in input_schema.properties")


def require_credential_references(value) -> None:
    pending = [value]
    secret_fields = {"api_key", "apikey", "password", "token", "authorization", "signing_secret"}
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in secret_fields and isinstance(child, str) and child:
                    raise CapabilityError("Capability definitions must use credential references, not embedded secrets")
                pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)


def _parse(kind: CapabilityKind, snapshot: str) -> Definition:
    models = {
        CapabilityKind.AGENT: AgentDefinition,
        CapabilityKind.WORKFLOW: WorkflowDefinition,
        CapabilityKind.TOOL: ToolDefinition,
        CapabilityKind.PROMPT: PromptDefinition,
        CapabilityKind.SKILL: SkillDefinition,
    }
    if kind == CapabilityKind.WORKFLOW:
        value = json.loads(snapshot)
        model = CodeWorkflowDefinition if value.get("definition_version") == "workflow.code.v1" else WorkflowDefinition
        return model.model_validate(value)
    if kind == CapabilityKind.MODEL_POLICY:
        value = json.loads(snapshot)
        model = AgentTurnPolicyDefinition if value.get("interaction") == "agent_turn" else ModelPolicyDefinition
        return model.model_validate(value)
    return models[kind].model_validate_json(snapshot)


def interpret_definition(kind: CapabilityKind | str, artifact: CapabilityVersionDraft) -> DefinitionInterpretation:
    """Validate executable semantics without SQL, schema processes or runtime I/O."""
    kind = CapabilityKind(kind)
    snapshot = canonical_json(artifact.definition)
    require_credential_references(artifact.definition)
    definition = _parse(kind, snapshot)
    references: list[DefinitionReference] = []
    profile = f"enterprise-ai.{kind.value}.interpreter.v1"

    def reference(ref, expected, role="resource"):
        references.append(DefinitionReference(ref.key, ref.version, CapabilityKind(expected), role))

    if isinstance(definition, PromptDefinition):
        validate_prompt_template(definition.template, artifact.input_schema.get("properties", {}))
    elif isinstance(definition, AgentDefinition):
        reference(definition.instruction_prompt, "prompt", "instruction")
        reference(definition.model_policy, "model_policy", "agent_turn")
        for tool in definition.tools:
            reference(tool.capability, "tool")
        for skill in definition.skills:
            reference(skill, "skill")
    elif isinstance(definition, (WorkflowDefinition, CodeWorkflowDefinition)):
        # This contract is also used by the compiler. Publication must never
        # approve retries which the installed interpreter cannot perform safely.
        if isinstance(definition, WorkflowDefinition):
            definition.require_execution_contract()
            profile = WORKFLOW_INTERPRETER_PROFILE
        else:
            profile = CODE_WORKFLOW_INTERPRETER_PROFILE
        expected = {"agent": "agent", "tool": "tool", "llm": "model_policy", "workflow": "workflow"}
        activities = definition.activities if isinstance(definition, CodeWorkflowDefinition) else definition.nodes
        for node in activities:
            if node.capability is not None:
                role = "code_workflow" if node.kind == "workflow" else "completion" if node.kind == "llm" else "resource"
                reference(node.capability, expected[node.kind], role)
            if node.prompt is not None:
                reference(node.prompt, "prompt")
    elif isinstance(definition, AgentTurnPolicyDefinition):
        profile = "enterprise-ai.model_policy.agent-turn.v1"
    elif isinstance(definition, ModelPolicyDefinition):
        profile = "enterprise-ai.model_policy.completion.v1"

    declared = [(item.key, item.version) for item in artifact.dependencies]
    if len(set(declared)) != len(declared):
        raise CapabilityError("Duplicate capability dependencies")
    if any((item.key, item.version) not in set(declared) for item in references):
        raise CapabilityError("Capability resources must be pinned in dependencies")
    return DefinitionInterpretation(kind, profile, snapshot, tuple(references))
