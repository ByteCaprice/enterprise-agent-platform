"""Publication use cases; interpretation and persistence are separate owners."""

from __future__ import annotations

from string import Template

from pydantic import TypeAdapter

from domain.capability import (
    CapabilityError,
    CapabilityKind,
    CapabilitySpec,
    CapabilityVersionDraft,
    Key,
)
from domain.capability_definitions import interpret_definition
from domain.capability_publication import PublicationDependency, PublicationEvidence
from domain.schema_validation import JSONSchemaInspector
from orchestration.capability_validation import CapabilityValidator


class CapabilityService:
    def __init__(self, repository, schemas: JSONSchemaInspector):
        self.repository = repository
        self.validation = CapabilityValidator(schemas)

    def create(self, project_key: str, spec: CapabilitySpec, actor: str):
        TypeAdapter(Key).validate_python(project_key)
        return self.repository.create(project_key, spec, actor)

    def create_draft(self, project_key: str, key: str, draft: CapabilityVersionDraft, actor: str):
        identity = self.repository.get(project_key, key)
        # Neither the caller nor an adapter may mutate content during validation.
        content_hash = draft.fingerprint()
        snapshot = CapabilityVersionDraft.model_validate_json(draft.model_dump_json())
        if snapshot.fingerprint() != content_hash:
            raise CapabilityError("Capability snapshot changed its canonical content")
        self.validation.validate(identity["kind"], snapshot)
        return self.repository.create_draft(project_key, key, snapshot, content_hash, actor)

    def publish(self, project_key: str, key: str, version: str, actor: str):
        identity = self.repository.get(project_key, key)
        artifact = self.repository.get_version(project_key, key, version).model_copy(deep=True)
        artifact.verify_integrity()
        if (artifact.capability_id, artifact.version) != (identity["id"], version):
            raise CapabilityError("Publication artifact does not match its capability identity")
        interpretation = self.validation.validate(identity["kind"], artifact)
        dependencies = []
        resolved = {}
        for reference in artifact.dependencies:
            if reference.key == key:
                raise CapabilityError("A capability cannot depend on itself")
            target = self.repository.get(project_key, reference.key)
            if target["project_key"] != project_key or target["key"] != reference.key:
                raise CapabilityError("Publication dependency identity does not match")
            dependency = self.repository.get_version(project_key, reference.key, reference.version).model_copy(deep=True)
            dependency.require_published(version=reference.version, capability_id=target["id"])
            contract = interpret_definition(target["kind"], dependency)
            resolved[(reference.key, reference.version)] = (target, dependency, contract)
            dependencies.append(PublicationDependency(
                reference.key, target["id"], CapabilityKind(target["kind"]), reference.version, dependency.content_hash,
            ))

        for required in interpretation.references:
            target, dependency, contract = resolved[(required.key, required.version)]
            if target["kind"] != required.kind:
                raise CapabilityError("Dependency capability kind does not match its definition contract")
            if required.role == "code_workflow" and contract.profile != "enterprise-ai.workflow.code.v1":
                raise CapabilityError("Code subworkflows require a code Workflow definition")
            if required.role in {"agent_turn", "completion"}:
                expected = "enterprise-ai.model_policy." + ("agent-turn.v1" if required.role == "agent_turn" else "completion.v1")
                if contract.profile != expected:
                    raise CapabilityError("Model policy interaction does not match its definition contract")
            if required.role == "instruction":
                prompt = contract.materialize()
                if Template(prompt.template).get_identifiers():
                    raise CapabilityError("Agent instructions must render without variables")
                self.validation.require_empty_variables(dependency.input_schema)

        # All subprocess/schema work finished before the repository takes locks.
        # The transaction rechecks these exact immutable identities, not just status.
        evidence = PublicationEvidence(
            artifact.capability_id, CapabilityKind(identity["kind"]), artifact.version, artifact.content_hash,
            interpretation.profile, tuple(dependencies),
        )
        return self.repository.publish(project_key, key, version, actor, evidence=evidence)
