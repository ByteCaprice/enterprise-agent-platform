"""Preflight and publish immutable hub resources through the normal capability API.

The batch is deliberately NOT atomic: each create/draft/publish remains a normal
repository transaction. A concurrent writer or later failure can leave a partial
publication. Retrying the same bundle safely reuses exact published versions.
This service never installs Python, creates providers/bindings/grants, or switches
an existing default. Standard first-publication default behavior is unchanged.
"""

from __future__ import annotations

from string import Template

from pydantic import TypeAdapter

from domain.capability import CapabilityConflict, CapabilityError, CapabilityKind, CapabilityNotFound, Key
from domain.capability_bundle import BundleCapability, CapabilityBundle
from domain.capability_definitions import interpret_definition
from domain.schema_validation import JSONSchemaInspector
from orchestration.capability_service import CapabilityService


class HubPublicationService:
    def __init__(self, repository, schemas: JSONSchemaInspector):
        self.repository = repository
        self.capabilities = CapabilityService(repository, schemas)

    @staticmethod
    def _identity(identity, project_key: str, key: str, kind=None):
        if identity["project_key"] != project_key or identity["key"] != key:
            raise CapabilityConflict("Hub publication capability identity does not match")
        if kind is not None and identity["kind"] != kind:
            raise CapabilityConflict("Hub publication capability kind conflicts with existing identity")

    @staticmethod
    def _version(artifact, identity, item: BundleCapability):
        artifact.verify_integrity()
        if (artifact.capability_id, artifact.version, artifact.content_hash) != (
                identity["id"], item.version, item.content_hash):
            raise CapabilityConflict("Hub publication version conflicts with existing content")
        if artifact.status not in {"draft", "published"}:
            raise CapabilityConflict("Hub publication requires a draft or published version")

    def _preflight(self, project_key: str, bundle: CapabilityBundle) -> None:
        resolved = {}
        for item in bundle.capabilities:
            interpretation = self.capabilities.validation.validate(item.kind, item.draft)
            resolved[item.key, item.version] = (item.kind, item.draft, interpretation)
            try:
                identity = self.repository.get(project_key, item.key)
            except CapabilityNotFound:
                continue
            self._identity(identity, project_key, item.key, item.kind)
            try:
                artifact = self.repository.get_version(project_key, item.key, item.version)
            except CapabilityNotFound:
                continue
            self._version(artifact, identity, item)

        for reference in bundle.external_dependencies:
            identity = self.repository.get(project_key, reference.key)
            self._identity(identity, project_key, reference.key)
            artifact = self.repository.get_version(project_key, reference.key, reference.version)
            artifact.require_published(version=reference.version, capability_id=identity["id"])
            resolved[reference.key, reference.version] = (
                CapabilityKind(identity["kind"]), artifact, interpret_definition(identity["kind"], artifact))

        # Do not defer role/kind errors until dependencies earlier in the batch
        # have already been written. CapabilityService revalidates at publication.
        for item in bundle.capabilities:
            _kind, _artifact, interpretation = resolved[item.key, item.version]
            for reference in interpretation.references:
                kind, artifact, target = resolved[reference.key, reference.version]
                if kind != reference.kind:
                    raise CapabilityError("Hub dependency kind does not match its definition contract")
                if reference.role == "code_workflow" and target.profile != "enterprise-ai.workflow.code.v1":
                    raise CapabilityError("Code subworkflows require a code Workflow definition")
                if reference.role in {"agent_turn", "completion"}:
                    expected = "enterprise-ai.model_policy." + (
                        "agent-turn.v1" if reference.role == "agent_turn" else "completion.v1")
                    if target.profile != expected:
                        raise CapabilityError("Hub model policy interaction does not match its definition contract")
                if reference.role == "instruction":
                    if Template(target.materialize().template).get_identifiers():
                        raise CapabilityError("Agent instructions must render without variables")
                    self.capabilities.validation.require_empty_variables(artifact.input_schema)

    def publish(self, project_key: str, bundle: CapabilityBundle, actor: str) -> dict:
        TypeAdapter(Key).validate_python(project_key)
        if not isinstance(bundle, CapabilityBundle):
            raise CapabilityError("Publish an explicit immutable CapabilityBundle")
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
            raise CapabilityError("Hub publication requires an explicit actor")
        self._preflight(project_key, bundle)
        results = []
        for item in bundle.capabilities:
            try:
                identity = self.repository.get(project_key, item.key)
            except CapabilityNotFound:
                try:
                    identity = self.capabilities.create(project_key, item.spec, actor)
                except CapabilityConflict:
                    identity = self.repository.get(project_key, item.key)
            self._identity(identity, project_key, item.key, item.kind)
            try:
                artifact = self.repository.get_version(project_key, item.key, item.version)
            except CapabilityNotFound:
                try:
                    artifact = self.capabilities.create_draft(project_key, item.key, item.draft, actor)
                except CapabilityConflict:
                    artifact = self.repository.get_version(project_key, item.key, item.version)
            self._version(artifact, identity, item)
            action = "reused" if artifact.status == "published" else "published"
            if artifact.status != "published":
                artifact = self.capabilities.publish(project_key, item.key, item.version, actor)
            self._version(artifact, identity, item)
            artifact.require_published(version=item.version, capability_id=identity["id"], content_hash=item.content_hash)
            results.append({"key": item.key, "kind": item.kind.value, "version": item.version,
                            "content_hash": item.content_hash, "status": "published", "action": action})
        return {"project_key": project_key, "bundle_key": bundle.key, "bundle_version": bundle.version,
                "atomic": False, "capabilities": results}
