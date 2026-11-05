"""Capability authoring and publication commands; execution uses read-only services."""

from __future__ import annotations

from string import Template

from pydantic import TypeAdapter
from pydantic import ValidationError as ModelValidationError

from domain.capability import (
    AgentTurnPolicyDefinition,
    CapabilityError,
    CapabilityKind,
    CapabilitySpec,
    CapabilityValidationUnavailable,
    CapabilityVersionDraft,
    Key,
    ModelPolicyDefinition,
    PromptDefinition,
    SkillDefinition,
)
from domain.platform_agent import AgentDefinition
from domain.platform_tool import ToolDefinition
from domain.schema_validation import JSONValidationError, JSONValidationUnavailable, ensure_self_contained_schema
from domain.task import canonical_json
from domain.workflow import WorkflowDefinition
from framework.prompts import validate_template


class CapabilityService:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _project(project_key):
        TypeAdapter(Key).validate_python(project_key)

    def create(self, project_key: str, spec: CapabilitySpec, actor: str):
        self._project(project_key)
        return self.repository.create(project_key, spec, actor)

    def create_draft(self, project_key: str, key: str, draft: CapabilityVersionDraft, actor: str):
        identity = self.repository.get(project_key, key)
        try:
            from infra.schema_validation import check_json_schema

            check_json_schema(draft.input_schema)
            check_json_schema(draft.output_schema)
        except JSONValidationUnavailable as exc:
            raise CapabilityValidationUnavailable(exc.code) from None
        except JSONValidationError:
            raise CapabilityError("Invalid capability JSON Schema") from None
        if draft.input_schema.get("type") != "object":
            raise CapabilityError("Capability input schema must describe an object")
        for schema in (draft.input_schema, draft.output_schema):
            self._local_schema_only(schema)
        self._credential_references_only(draft.definition)
        self._validate_definition(CapabilityKind(identity["kind"]), draft)
        refs = [(item.key, item.version) for item in draft.dependencies]
        if len(set(refs)) != len(refs):
            raise CapabilityError("Duplicate capability dependencies")
        content_hash = draft.fingerprint()
        return self.repository.create_draft(project_key, key, draft, content_hash, actor)

    @staticmethod
    def _credential_references_only(value):
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

    @staticmethod
    def _local_schema_only(value):
        try:
            ensure_self_contained_schema(value)
        except JSONValidationError:
            raise CapabilityError("Capability schemas require bounded local references") from None

    @staticmethod
    def _validate_schema(instance, schema) -> None:
        from infra.schema_validation import validate_json

        try:
            validate_json(instance, schema)
        except JSONValidationUnavailable as exc:
            raise CapabilityValidationUnavailable(exc.code) from None
        except JSONValidationError:
            raise CapabilityError("JSON value does not match the published schema") from None

    @staticmethod
    def _validate_definition(kind, draft):
        if kind == CapabilityKind.PROMPT:
            definition = PromptDefinition.model_validate(draft.definition)
            validate_template(definition.template, draft.input_schema.get("properties", {}))
        elif kind == CapabilityKind.TOOL:
            ToolDefinition.model_validate(draft.definition)
        elif kind == CapabilityKind.AGENT:
            definition = AgentDefinition.model_validate(draft.definition)
            declared = {(ref.key, ref.version) for ref in draft.dependencies}
            if any((ref.key, ref.version) not in declared for ref in definition.dependency_refs()):
                raise CapabilityError("Agent resources must be pinned in dependencies")
        elif kind == CapabilityKind.SKILL:
            SkillDefinition.model_validate(draft.definition)
        elif kind == CapabilityKind.WORKFLOW:
            try:
                canonical_json(draft.definition)
                workflow = WorkflowDefinition.model_validate(draft.definition)
                workflow.ordered_nodes()
            except ModelValidationError:
                raise CapabilityError("Invalid workflow definition") from None
            except ValueError as exc:
                raise CapabilityError(str(exc)) from None
            refs = {(item.key, item.version) for item in draft.dependencies}
            if any(reference is not None and (reference.key, reference.version) not in refs
                   for node in workflow.nodes for reference in (node.capability, node.prompt)):
                raise CapabilityError("Workflow node capabilities must be pinned in dependencies")
        elif kind == CapabilityKind.MODEL_POLICY:
            model = AgentTurnPolicyDefinition if draft.definition.get("interaction") == "agent_turn" else ModelPolicyDefinition
            model.model_validate(draft.definition)

    def publish(self, project_key: str, key: str, version: str, actor: str):
        # Revalidate persisted artifacts before publishing, not just at the HTTP boundary.
        artifact = self.repository.get_version(project_key, key, version)
        identity = self.repository.get(project_key, key)
        artifact.verify_integrity()
        self._validate_definition(CapabilityKind(identity["kind"]), artifact)
        if identity["kind"] == CapabilityKind.AGENT:
            definition = AgentDefinition.model_validate(artifact.definition)
            references = [(definition.instruction_prompt, "prompt"), (definition.model_policy, "model_policy"),
                          *((tool.capability, "tool") for tool in definition.tools),
                          *((skill, "skill") for skill in definition.skills)]
            for reference, expected in references:
                if self.repository.get(project_key, reference.key)["kind"] != expected:
                    raise CapabilityError("Agent dependency has the wrong resource kind")
                if expected == "skill":
                    skill = self.repository.get_version(project_key, reference.key, reference.version)
                    SkillDefinition.model_validate(skill.definition)
            policy = self.repository.get_version(project_key, definition.model_policy.key, definition.model_policy.version)
            AgentTurnPolicyDefinition.model_validate(policy.definition)
            prompt = self.repository.get_version(project_key, definition.instruction_prompt.key, definition.instruction_prompt.version)
            template = Template(PromptDefinition.model_validate(prompt.definition).template)
            if not template.is_valid() or template.get_identifiers():
                raise CapabilityError("Agent instructions must render without variables")
            try:
                self._local_schema_only(prompt.input_schema)
                self._validate_schema({}, prompt.input_schema)
            except CapabilityValidationUnavailable:
                raise
            except CapabilityError:
                raise CapabilityError("Agent instruction Prompt must accept an empty variable object") from None
        if identity["kind"] == CapabilityKind.WORKFLOW:
            expected_kinds = {"agent": "agent", "tool": "tool", "llm": "model_policy"}
            for node in WorkflowDefinition.model_validate(artifact.definition).nodes:
                for reference, expected in ((node.capability, expected_kinds.get(node.kind)), (node.prompt, "prompt")):
                    if reference is not None and self.repository.get(project_key, reference.key)["kind"] != expected:
                        raise CapabilityError("Workflow dependency capability kind does not match its node contract")
                    if reference is not None and expected == "model_policy":
                        policy = self.repository.get_version(project_key, reference.key, reference.version)
                        ModelPolicyDefinition.model_validate(policy.definition)
                    if reference is not None and expected == "agent":
                        child = self.repository.get_version(project_key, reference.key, reference.version)
                        AgentDefinition.model_validate(child.definition)
        return self.repository.publish(project_key, key, version, actor)
