"""Pure capability validation boundaries; no database or network required."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from domain.capability import (
    CapabilityConflict,
    CapabilityDependency,
    CapabilityError,
    CapabilityKind,
    CapabilityVersionDraft,
    CapabilityVersionRecord,
)
from framework.prompts import render_template
from orchestration.capability_service import CapabilityService
from orchestration.prompt_renderer import PromptRenderer
from orchestration.published_capabilities import PublishedCapabilities


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_all_schema_reference_forms_refuse_external_fetch(keyword):
    with pytest.raises(CapabilityError, match="local references"):
        CapabilityService._local_schema_only({"$defs": {"nested": {keyword: "https://untrusted.test/schema"}}})


def test_artifact_fingerprint_excludes_publication_metadata_and_rejects_nul():
    draft = CapabilityVersionDraft(version="1", definition={"template": "Hello"})
    canonical = ('{"definition":{"template":"Hello"},"dependencies":[],"input_schema":{"type":"object"},'
                 '"output_schema":{"type":"object"},"version":"1"}')
    expected = hashlib.sha256(canonical.encode()).hexdigest()
    assert draft.fingerprint() == expected
    record = CapabilityVersionRecord.model_validate({**draft.model_dump(), "id": "version", "capability_id": "cap",
        "status": "draft", "content_hash": expected, "created_by": "operator", "create_time": datetime.now(UTC)})
    record.status = "published"
    assert record.fingerprint() == expected
    draft.definition["private"] = "\x00"
    with pytest.raises(CapabilityError):
        draft.fingerprint()


def test_local_schema_references_are_allowed():
    CapabilityService._local_schema_only({"$defs": {"value": {"type": "string"}}, "$ref": "#/$defs/value"})


@pytest.mark.parametrize("nodes", [
    [{"key": "a", "depends_on": ["b"]}, {"key": "b", "depends_on": ["a"]}],
    [{"key": "a", "depends_on": ["unknown"]}],
    [{"key": "a"}, {"key": "a"}],
])
def test_workflow_contract_rejects_cycles_missing_nodes_and_duplicate_keys(nodes):
    definition = {"nodes": [
        {"kind": "tool", "capability": {"key": "lookup", "version": "1"}, **node} for node in nodes
    ]}
    draft = CapabilityVersionDraft(version="1", definition=definition, dependencies=[{"key": "lookup", "version": "1"}])
    with pytest.raises(CapabilityError, match="cycle|dependency|unique"):
        CapabilityService._validate_definition(CapabilityKind.WORKFLOW, draft)


def test_workflow_cannot_implicitly_resolve_an_unpinned_node_capability():
    draft = CapabilityVersionDraft(version="1", definition={"nodes": [
        {"key": "lookup", "kind": "tool", "capability": {"key": "lookup", "version": "1"}},
    ]})
    with pytest.raises(CapabilityError, match="pinned"):
        CapabilityService._validate_definition(CapabilityKind.WORKFLOW, draft)


def published_prompt():
    draft = CapabilityVersionDraft(version="1", definition={"template": "Hello ${name}"}, input_schema={
        "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False,
    })
    artifact = CapabilityVersionRecord(**draft.model_dump(), id="version", capability_id="prompt",
        status="published", content_hash=draft.fingerprint(), created_by="author", create_time=datetime.now(UTC))
    identity = {"id": "prompt", "project_key": "demo", "key": "greeting", "kind": "prompt", "default_version": "2"}
    calls = []

    def get_version(project, key, version):
        calls.append((project, key, version))
        return artifact

    return SimpleNamespace(get=lambda *_: identity, get_version=get_version), artifact, identity, calls


def test_published_reader_uses_exact_identity_version_and_detached_content():
    source, artifact, identity, calls = published_prompt()
    catalog = PublishedCapabilities(source)
    reference = CapabilityDependency(key="greeting", version="1")
    resolved = catalog.resolve("demo", reference, CapabilityKind.PROMPT)
    assert calls == [("demo", "greeting", "1")]  # Never follow mutable default_version.
    resolved.definition["template"] = "Changed in consumer"
    assert artifact.definition["template"] == "Hello ${name}"
    with pytest.raises(CapabilityConflict):
        resolved.verify_integrity()
    identity["project_key"] = "other"
    with pytest.raises(CapabilityError, match="identity"):
        catalog.resolve("demo", reference, CapabilityKind.PROMPT)


@pytest.mark.parametrize("changes", [{"status": "draft"}, {"version": "2"}, {"capability_id": "other"},
                                    {"content_hash": "0" * 64}, {"definition": {"template": "Tampered"}}])
def test_published_reader_rejects_unpublished_misbound_or_corrupt_artifacts(changes):
    source, artifact, _, _ = published_prompt()
    for key, value in changes.items():
        setattr(artifact, key, value)
    with pytest.raises(CapabilityConflict):
        PublishedCapabilities(source).resolve("demo", CapabilityDependency(key="greeting", version="1"), CapabilityKind.PROMPT)


def test_prompt_rendering_needs_only_read_port_and_pure_template_rules():
    source, artifact, _, _ = published_prompt()
    result = PromptRenderer(source).render("demo", "greeting", "1", {"name": "World"})
    assert result["text"] == "Hello World" and result["content_hash"] == artifact.content_hash
    assert result["rendered_hash"] == hashlib.sha256(b"Hello World").hexdigest()
    assert render_template("${value} $$", {"value": {"ok": True}}) == '{"ok": true} $'
    with pytest.raises(CapabilityError, match="variables"):
        PromptRenderer(source).render("demo", "greeting", "1", {})
