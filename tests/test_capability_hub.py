"""Offline hub publication and real caller-supplied candidate business operations."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import jsonschema
import pytest

from capability_hub import builtin_catalog, load_catalog
from capability_hub.remitter_search.bundle import SOURCE
from capability_hub.remitter_search.schemas import SearchOutput
from capability_hub.remitter_search.steps import normalize_name, normalize_search, rank_candidates, validate_selection
from capability_hub.remitter_search.tools import lookup_candidates
from capability_hub.remitter_search.workflow import AGENT_ACTIVITY, NORMALIZE_ACTIVITY, VALIDATE_ACTIVITY
from domain.business_code import ActivityOutcome, CodeReference, CodeWorkflowDefinition
from domain.capability import (
    CapabilityConflict,
    CapabilityDependency,
    CapabilityError,
    CapabilityKind,
    CapabilityNotFound,
    CapabilitySpec,
    CapabilityVersionDraft,
    CapabilityVersionRecord,
)
from domain.capability_bundle import BundleCapability, CapabilityBundle
from domain.capability_definitions import interpret_definition
from domain.schema_validation import JSONValidationError
from domain.task_execution import ExecutionFailure
from framework.business_code import CodeWorkflowRegistryError
from orchestration.hub_service import HubPublicationService
from orchestration.workflow_plan_query import WorkflowPlanQuery

POLICY = CapabilityDependency(key="operator-search-policy", version="approved-7")


def subworkflow_bundle(*, code_child):
    reference = CodeReference(key="generic-flow", version="1", digest="a" * 64)
    child_definition = ({"definition_version": "workflow.code.v1", "implementation": reference.model_dump(),
                         "activities": [{"key": "compute", "kind": "step"}]} if code_child else
                        {"nodes": [{"key": "compute", "kind": "rule", "rule": {"operation": "project"}}]})
    child = BundleCapability(CapabilitySpec(key="child-flow", kind="workflow", name="Child"),
                             CapabilityVersionDraft(version="1", definition=child_definition))
    parent = BundleCapability(CapabilitySpec(key="parent-flow", kind="workflow", name="Parent"),
        CapabilityVersionDraft(version="1", definition=CodeWorkflowDefinition.model_validate({
            "definition_version": "workflow.code.v1", "implementation": reference.model_dump(),
            "activities": [{"key": "delegate", "kind": "workflow",
                            "capability": {"key": child.key, "version": child.version}}],
        }).model_dump(mode="json"), dependencies=[{"key": child.key, "version": child.version}]))
    return CapabilityBundle("generic-flow", "1", "Generic flow", (parent, child))


def test_subworkflow_preflight_rejects_dsl_before_any_bundle_write():
    repository = MemoryCapabilities()
    with pytest.raises(CapabilityError, match="Code subworkflows"):
        HubPublicationService(repository, LocalSchemas()).publish("demo", subworkflow_bundle(code_child=False), "ops")
    assert repository.writes == []


def test_code_subworkflow_plan_exposes_declaration_not_a_fabricated_adapter_or_dag():
    repository = MemoryCapabilities()
    HubPublicationService(repository, LocalSchemas()).publish("demo", subworkflow_bundle(code_child=True), "ops")
    plan = WorkflowPlanQuery(repository).describe("demo", "parent-flow", "1")
    assert plan.required_adapters == [] and plan.max_parallelism == 4
    assert plan.activities[0].kind == "workflow"
    assert "nodes" not in plan.model_dump() and not plan.supports_resume


class LocalSchemas:
    """In-process test port; production uses the existing bounded schema inspector."""

    def check_schema(self, schema):
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError:
            raise JSONValidationError("SCHEMA_INVALID") from None

    def validate(self, instance, schema):
        try:
            jsonschema.Draft202012Validator(schema).validate(instance)
        except jsonschema.ValidationError:
            raise JSONValidationError("INSTANCE_INVALID") from None


class MemoryCapabilities:
    """Publication repository test double, never a business endpoint or runtime."""

    def __init__(self):
        self.identities = {}
        self.versions = {}
        self.writes = []

    def get(self, project, key):
        try:
            return copy.deepcopy(self.identities[project, key])
        except KeyError:
            raise CapabilityNotFound("Capability not found") from None

    def get_version(self, project, key, version):
        try:
            return self.versions[project, key, version].model_copy(deep=True)
        except KeyError:
            raise CapabilityNotFound("Capability version not found") from None

    def create(self, project, spec, actor):
        if (project, spec.key) in self.identities:
            raise CapabilityConflict("Capability already exists")
        identity = {**spec.model_dump(mode="json"), "id": f"{project}:{spec.key}",
                    "project_key": project, "default_version": None, "created_by": actor}
        self.identities[project, spec.key] = identity
        self.writes.append(("create", spec.key))
        return self.get(project, spec.key)

    def create_draft(self, project, key, draft, content_hash, actor):
        if (project, key, draft.version) in self.versions:
            raise CapabilityConflict("Version already exists")
        artifact = CapabilityVersionRecord(**draft.model_dump(), id=f"{key}:{draft.version}",
            capability_id=self.get(project, key)["id"], content_hash=content_hash,
            status="draft", created_by=actor, create_time=datetime.now(UTC))
        self.versions[project, key, draft.version] = artifact
        self.writes.append(("draft", key))
        return artifact.model_copy(deep=True)

    def publish(self, project, key, version, actor, *, evidence):
        artifact = self.versions[project, key, version]
        assert artifact.content_hash == evidence.content_hash
        for dependency in evidence.dependencies:
            self.get_version(project, dependency.key, dependency.version).require_published(
                capability_id=dependency.capability_id, content_hash=dependency.content_hash)
        artifact.status = "published"
        artifact.published_by = actor
        artifact.publish_time = datetime.now(UTC)
        identity = self.identities[project, key]
        if identity["default_version"] is None:
            identity["default_version"] = version
        self.writes.append(("publish", key))
        return artifact.model_copy(deep=True)


def with_policy(*, interaction="agent_turn", status="published", kind="model_policy"):
    repository = MemoryCapabilities()
    spec = CapabilitySpec(key=POLICY.key, kind=kind, name="Operator-created policy")
    repository.create("search-project", spec, "operator")
    definition = ({"interaction": interaction, "provider_key": "operator-provider", "model": "operator-model"}
                  if kind == "model_policy" else {"template": "Not a model policy"})
    draft = CapabilityVersionDraft(version=POLICY.version, definition=definition)
    repository.create_draft("search-project", POLICY.key, draft, draft.fingerprint(), "operator")
    repository.versions["search-project", POLICY.key, POLICY.version].status = status
    repository.writes.clear()
    return repository


def resource(key, *, dependencies=(), template="Reviewed text", kind="prompt"):
    return BundleCapability(CapabilitySpec(key=key, kind=kind, name=key), CapabilityVersionDraft(
        version="1", definition={"template": template}, dependencies=[
            CapabilityDependency(key=key, version="1") for key in dependencies]))


def test_same_repo_import_explicit_catalog_and_source_closure():
    assert builtin_catalog().keys == ("remitter-search", "weather-query")
    catalog = builtin_catalog().select(("remitter-search",))
    package = resources.files("capability_hub.remitter_search")
    assert isinstance(package, Path)
    assert package.resolve().is_relative_to(Path(__file__).resolve().parents[1] / "capability_hub")
    assert set(SOURCE.files) == {file.name for file in package.glob("*.py")}
    assert {"agent.py", "workflow.py", "steps.py", "tools.py", "schemas.py", "prompts.py"} <= set(SOURCE.files)
    assert catalog.keys == ("remitter-search",)
    assert load_catalog().select(("remitter-search",)).references == catalog.references
    assert len(catalog.references) == 2
    assert {ref.key for ref in catalog.references} == {"remitter-search", "remitter-search.lookup"}
    assert catalog.tools[0].implementation in catalog.references
    assert catalog.select(()).keys == ()
    assert catalog.select(()).workflows.references == ()
    assert catalog.select(()).tools == ()
    assert catalog.select(["remitter-search", "remitter-search"]).keys == catalog.keys
    with pytest.raises(CapabilityNotFound):
        catalog.select(["invented-business"])
    with pytest.raises(ValueError):
        catalog.select("remitter-search")
    description = json.dumps(catalog.describe())
    assert "operator-provider" not in description and "model_policy" not in description


def test_bundle_pins_resources_external_policy_and_no_import_targets():
    catalog = builtin_catalog().select(("remitter-search",))
    bundle = catalog.build("remitter-search", policy=POLICY)
    assert bundle.version == "1" and bundle.implementations == bundle.references == catalog.references
    assert [(ref.key, ref.version) for ref in bundle.external_dependencies] == [(POLICY.key, POLICY.version)]
    positions = {(item.key, item.version): index for index, item in enumerate(bundle.capabilities)}
    for index, item in enumerate(bundle.capabilities):
        assert item.version == "1"
        interpretation = interpret_definition(item.kind, item.draft)
        assert interpretation.materialize() is not None
        for dependency in item.draft.dependencies:
            if (dependency.key, dependency.version) in positions:
                assert positions[dependency.key, dependency.version] < index
    exported = bundle.export()
    definitions = {item.key: item.draft.definition for item in bundle.capabilities}
    assert definitions["remitter-search-agent"]["runtime"] == "pi"
    assert definitions["remitter-search-agent"]["model_policy"] == POLICY.model_dump()
    assert definitions["remitter-search.lookup"]["definition_version"] == "tool.code.v1"
    assert definitions["remitter-search"]["definition_version"] == "workflow.code.v1"
    assert "capability_hub.remitter_search" not in json.dumps(exported)
    assert all("provider_key" not in item.draft.definition for item in bundle.capabilities)


def test_bundle_owns_immutable_canonical_snapshots_not_nested_models():
    spec = CapabilitySpec(key="reviewed", kind="prompt", name="Reviewed")
    draft = CapabilityVersionDraft(version="1", definition={"template": "Original"})
    entry = BundleCapability(spec, draft)
    bundle = CapabilityBundle("reviewed-bundle", "1", "Reviewed bundle", [entry])
    original = bundle.fingerprint()
    spec.kind = "tool"
    draft.definition["template"] = "Caller mutated"
    entry.spec.name = "Consumer mutated"
    entry.draft.definition["template"] = "Consumer mutated"
    bundle.export()["capabilities"][0]["draft"]["definition"]["template"] = "Export mutated"
    assert entry.kind == "prompt" and entry.draft.definition["template"] == "Original"
    assert bundle.fingerprint() == original
    with pytest.raises(FrozenInstanceError):
        bundle.version = "2"


def test_bundle_rejects_cycles_kind_conflicts_and_content_conflicts():
    with pytest.raises(CapabilityError, match="cycle"):
        CapabilityBundle("cyclic", "1", "Cycle", [resource("first", dependencies=("second",)),
                                                     resource("second", dependencies=("first",))])
    with pytest.raises(CapabilityConflict, match="kinds"):
        CapabilityBundle("conflict", "1", "Conflict", [resource("same"), resource("same", kind="tool")])
    with pytest.raises(CapabilityConflict, match="content"):
        CapabilityBundle("conflict", "1", "Conflict", [resource("same"), resource("same", template="Other")])
    ordered = CapabilityBundle("ordered", "1", "Ordered", [resource("later", dependencies=("earlier",)), resource("earlier")])
    assert [item.key for item in ordered.capabilities] == ["earlier", "later"]


@pytest.mark.parametrize("changed_file", ["prompts.py", "steps.py", "tools.py"])
def test_source_edits_change_references_and_stale_catalogs_reject_them(tmp_path, monkeypatch, changed_file):
    from framework import business_code

    catalog = builtin_catalog().select(("remitter-search",))
    source_root = resources.files(SOURCE.package)
    for filename in SOURCE.files:
        (tmp_path / filename).write_bytes((source_root / filename).read_bytes())
    original_files = business_code.resources.files
    monkeypatch.setattr(business_code.resources, "files", lambda package: (
        tmp_path if package == SOURCE.package else original_files(package)))
    old_reference = catalog.workflows.references[0]
    assert SOURCE.reference(old_reference.key, old_reference.version) == old_reference
    path = tmp_path / changed_file
    path.write_bytes(path.read_bytes() + b"\n# reviewed source changed\n")
    assert SOURCE.reference(old_reference.key, old_reference.version).digest != old_reference.digest
    with pytest.raises(CodeWorkflowRegistryError, match="digest"):
        catalog.workflows.resolve(old_reference)
    with pytest.raises(CodeWorkflowRegistryError, match="digest"):
        catalog.build("remitter-search", policy=POLICY)


def test_publication_is_topological_idempotent_and_does_not_switch_existing_defaults():
    repository = with_policy()
    bundle = builtin_catalog().build("remitter-search", policy=POLICY)
    first = bundle.capabilities[0]
    repository.create("search-project", first.spec, "operator")
    repository.identities["search-project", first.key]["default_version"] = "operator-selected-version"
    repository.writes.clear()
    service = HubPublicationService(repository, LocalSchemas())
    result = service.publish("search-project", bundle, "authenticated-admin")
    assert result["atomic"] is False
    assert [key for operation, key in repository.writes if operation == "publish"] == [item.key for item in bundle.capabilities]
    assert {item["action"] for item in result["capabilities"]} == {"published"}
    assert repository.get("search-project", first.key)["default_version"] == "operator-selected-version"
    writes = list(repository.writes)
    repeated = service.publish("search-project", bundle, "another-admin")
    assert repository.writes == writes
    assert {item["action"] for item in repeated["capabilities"]} == {"reused"}
    assert all(repository.get_version("search-project", item.key, item.version).content_hash == item.content_hash
               for item in bundle.capabilities)
    assert len(repository.identities) == 5  # Four resources plus the already-existing operator policy.


@pytest.mark.parametrize("existing", ["different-kind", "different-hash", "invalid-hash"])
def test_publication_preflights_late_conflicts_before_writing_any_earlier_resource(existing):
    repository = with_policy()
    bundle = builtin_catalog().build("remitter-search", policy=POLICY)
    last = bundle.capabilities[-1]
    spec = last.spec
    if existing == "different-kind":
        spec.kind = CapabilityKind.PROMPT
    repository.create("search-project", spec, "operator")
    if existing != "different-kind":
        draft = last.draft
        if existing == "different-hash":
            draft.definition["implementation"]["digest"] = "0" * 64
        repository.create_draft("search-project", last.key, draft, draft.fingerprint(), "operator")
        if existing == "invalid-hash":
            repository.versions["search-project", last.key, last.version].content_hash = "0" * 64
    repository.writes.clear()
    with pytest.raises(CapabilityConflict):
        HubPublicationService(repository, LocalSchemas()).publish("search-project", bundle, "admin")
    assert repository.writes == []


@pytest.mark.parametrize("policy_state", ["missing", "draft", "completion", "wrong-kind", "corrupt"])
def test_external_policy_must_already_be_published_agent_turn_before_any_writes(policy_state):
    repository = with_policy(interaction="completion" if policy_state == "completion" else "agent_turn",
                             status="draft" if policy_state == "draft" else "published",
                             kind="prompt" if policy_state == "wrong-kind" else "model_policy")
    if policy_state == "missing":
        repository.identities.clear()
        repository.versions.clear()
    elif policy_state == "corrupt":
        repository.versions["search-project", POLICY.key, POLICY.version].content_hash = "0" * 64
    bundle = builtin_catalog().build("remitter-search", policy=POLICY)
    with pytest.raises(CapabilityError):
        HubPublicationService(repository, LocalSchemas()).publish("search-project", bundle, "admin")
    assert repository.writes == []


def test_matching_existing_drafts_resume_publication_without_overwrite():
    repository = with_policy()
    bundle = builtin_catalog().build("remitter-search", policy=POLICY)
    item = bundle.capabilities[0]
    repository.create("search-project", item.spec, "operator")
    repository.create_draft("search-project", item.key, item.draft, item.content_hash, "original-author")
    repository.writes.clear()
    HubPublicationService(repository, LocalSchemas()).publish("search-project", bundle, "admin")
    assert ("draft", item.key) not in repository.writes
    assert repository.get_version("search-project", item.key, item.version).created_by == "original-author"


def candidate_values():
    # Synthetic test data only; no source catalog contains customer records.
    return {"query": "  Café---North  ", "candidates": [
        {"id": "candidate-b", "name": "Cafe North", "aliases": []},
        {"id": "candidate-z", "name": "Unrelated South", "aliases": []},
        {"id": "candidate-a", "name": "CAFÉ NORTH", "aliases": ["North Cafe"]}], "limit": 2}


def test_pure_normalization_ranking_and_async_readonly_tool_are_deterministic():
    values = candidate_values()
    original = copy.deepcopy(values)
    assert normalize_name("  Café---North  ") == "cafe north"
    normalized = normalize_search(values)
    assert normalized["query"] == "cafe north"
    assert normalize_search(normalized) == normalized
    ranked = rank_candidates(values)
    assert [match["candidate_id"] for match in ranked["matches"]] == ["candidate-a", "candidate-b"]
    assert [match["score"] for match in ranked["matches"]] == [1000, 1000]
    assert asyncio.run(lookup_candidates(None, values)) == ranked
    assert values == original
    assert rank_candidates({"query": "qwerty", "candidates": values["candidates"]})["matches"] == []


class LocalWorkflowContext:
    """Execute installed business steps while controlling only the model boundary."""

    def __init__(self, installed, selection=None):
        self.installed = installed
        self.selection = selection
        self.events = []

    async def call(self, activity, values, *, key="once"):
        if activity in self.installed.steps:
            self.events.append(("step", activity))
            return ActivityOutcome("succeeded", await self.installed.steps[activity](None, values))
        self.events.append(("agent", activity))
        assert values["query"] == "cafe north"
        assert self.selection is not None, "A pure branch must not require a model"
        return ActivityOutcome("succeeded", self.selection)

    async def skip(self, activity, *, key="once"):
        self.events.append(("skip", activity))
        return ActivityOutcome("skipped")


@pytest.mark.parametrize("values", [{}, {"query": "any name"}, {"query": "   ", "candidates": candidate_values()["candidates"]}])
def test_pure_empty_branches_need_no_model(values):
    catalog = builtin_catalog().select(("remitter-search",))
    installed = catalog.workflows.resolve(catalog.workflows.references[0])
    context = LocalWorkflowContext(installed)
    result = asyncio.run(installed.run(context, values))
    assert result["selected_candidate_ids"] == [] and result["matches"] == []
    assert result["selection_method"] == "empty_input"
    assert "not identity verification or compliance certification" in result["notice"]
    assert context.events == [("step", NORMALIZE_ACTIVITY), ("skip", AGENT_ACTIVITY), ("step", VALIDATE_ACTIVITY)]
    LocalSchemas().validate(result, SearchOutput.model_json_schema())


def test_mixed_workflow_runs_normalization_agent_and_custom_validation():
    catalog = builtin_catalog().select(("remitter-search",))
    installed = catalog.workflows.resolve(catalog.workflows.references[0])
    context = LocalWorkflowContext(installed, {"selected_candidate_ids": ["candidate-b", "candidate-a"]})
    result = asyncio.run(installed.run(context, candidate_values()))
    assert result["selected_candidate_ids"] == ["candidate-a", "candidate-b"]
    assert result["selection_method"] == "agent_assisted" and result["query"] == "cafe north"
    assert context.events == [("step", NORMALIZE_ACTIVITY), ("agent", AGENT_ACTIVITY), ("step", VALIDATE_ACTIVITY)]
    LocalSchemas().validate(result, SearchOutput.model_json_schema())


@pytest.mark.parametrize("selected", [["not-supplied"], ["candidate-z"]])
def test_model_cannot_select_invented_or_nonmatching_supplied_candidates(selected):
    with pytest.raises(ExecutionFailure, match="REMITTER_SELECTION_INVALID"):
        validate_selection({"search": candidate_values(), "selection": {"selected_candidate_ids": selected}})


def test_duplicate_ids_and_model_extra_fields_fail_closed():
    values = candidate_values()
    values["candidates"].append(values["candidates"][0])
    with pytest.raises(ValueError, match="unique"):
        normalize_search(values)
    with pytest.raises(ValueError, match="unique"):
        validate_selection({"search": candidate_values(), "selection": {"selected_candidate_ids": ["candidate-a", "candidate-a"]}})
    with pytest.raises(ValueError):
        validate_selection({"search": candidate_values(), "selection": {"selected_candidate_ids": [], "certified": True}})
