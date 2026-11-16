"""Versioned capabilities and Prompt Bank with real isolated PostgreSQL storage."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from conftest import make_store
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domain import ApiKeyConfig
from domain.capability import (
    CapabilityConflict,
    CapabilityError,
    CapabilityNotFound,
    CapabilitySpec,
    CapabilityVersionDraft,
)
from domain.governance import ProjectCreate
from infra.schema_validation import IsolatedJSONValidator
from interfaces.http.routes.capabilities import router
from orchestration.capability_service import CapabilityService
from orchestration.control_plane_auth import ControlPlaneAuth
from orchestration.prompt_renderer import PromptRenderer

pytestmark = pytest.mark.usefixtures("pg_url")


def draft(version="1.0.0", template="Review ${name}. Return JSON.", **changes):
    return CapabilityVersionDraft.model_validate({
        "version": version,
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                         "required": ["name"], "additionalProperties": False},
        "definition": {"template": template}, **changes,
    })


def bank():
    store = make_store()
    store.governance.create_project(ProjectCreate(key="payments", name="Payments"), "admin")
    service = CapabilityService(store.capabilities, IsolatedJSONValidator())
    service.create("payments", CapabilitySpec(key="review", kind="prompt", name="Review prompt"), "admin")
    return store, service


def test_prompt_draft_publish_render_and_atomic_audit():
    store, service = bank()
    created = service.create_draft("payments", "review", draft(), "admin")
    assert created.status == "draft"
    assert store.capabilities.get("payments", "review")["default_version"] is None
    published = service.publish("payments", "review", "1.0.0", "publisher")
    result = PromptRenderer(store.capabilities).render("payments", "review", "1.0.0", {"name": "Synthetic Co"})

    assert result["text"] == "Review Synthetic Co. Return JSON."
    assert result["content_hash"] == published.content_hash == created.content_hash
    assert len(result["rendered_hash"]) == 64
    assert published.published_by == "publisher"
    assert store.capabilities.get("payments", "review")["default_version"] == "1.0.0"
    assert {event.event_type for event in store.logs.list_by_component("capability_audit")} == {
        "capability_created", "capability_draft_created", "capability_published",
    }
    assert all("template" not in event.data for event in store.logs.list_by_component("capability_audit"))


def test_existing_version_cannot_be_overwritten_even_before_publication():
    _store, service = bank()
    original = service.create_draft("payments", "review", draft(), "admin")
    with pytest.raises(CapabilityConflict, match="already exists"):
        service.create_draft("payments", "review", draft(template="Changed ${name}"), "other")
    assert service.repository.get_version("payments", "review", "1.0.0") == original


def test_default_rollback_does_not_change_published_artifacts():
    store, service = bank()
    for version in ("1.0.0", "2.0.0"):
        service.create_draft("payments", "review", draft(version, f"Version {version}: ${{name}}"), "admin")
        service.publish("payments", "review", version, "admin")
    old = store.capabilities.get_version("payments", "review", "1.0.0")
    store.capabilities.set_default("payments", "review", "2.0.0", "admin")
    store.capabilities.set_default("payments", "review", "1.0.0", "admin")
    assert store.capabilities.get_version("payments", "review", "1.0.0") == old
    assert PromptRenderer(store.capabilities).render("payments", "review", "2.0.0", {"name": "X"})["text"] == "Version 2.0.0: X"


def test_drafts_cannot_be_default_or_rendered():
    store, service = bank()
    service.create_draft("payments", "review", draft(), "admin")
    with pytest.raises(CapabilityConflict, match="published"):
        store.capabilities.set_default("payments", "review", "1.0.0", "admin")
    with pytest.raises(CapabilityConflict, match="published"):
        PromptRenderer(store.capabilities).render("payments", "review", "1.0.0", {"name": "X"})


@pytest.mark.parametrize("variables", [{}, {"name": 123}, {"name": "X", "undeclared": True}])
def test_prompt_input_contract_is_enforced(variables):
    store, service = bank()
    service.create_draft("payments", "review", draft(), "admin")
    service.publish("payments", "review", "1.0.0", "admin")
    with pytest.raises(CapabilityError, match="variables"):
        PromptRenderer(store.capabilities).render("payments", "review", "1.0.0", variables)


@pytest.mark.parametrize("template", ["Missing ${undeclared}", "Invalid ${name", "Invalid $!"])
def test_invalid_or_undeclared_template_variables_are_rejected(template):
    _store, service = bank()
    with pytest.raises(CapabilityError, match="variables|syntax"):
        service.create_draft("payments", "review", draft(template=template), "admin")


def test_remote_json_schema_references_are_rejected_without_network_access():
    _store, service = bank()
    with pytest.raises(CapabilityError, match="local references"):
        service.create_draft("payments", "review", draft(
            output_schema={"$ref": "https://untrusted.test/schema.json"}), "admin")


def test_agent_definitions_cannot_embed_provider_credentials():
    store = make_store()
    store.governance.create_project(ProjectCreate(key="payments", name="Payments"), "admin")
    service = CapabilityService(store.capabilities, IsolatedJSONValidator())
    service.create("payments", CapabilitySpec(key="research", kind="agent", name="Research"), "admin")
    with pytest.raises(CapabilityError, match="credential references"):
        service.create_draft("payments", "research", CapabilityVersionDraft(
            version="1", definition={"name": "research", "route_tags": ["research"],
                                     "runtime": {"type": "pi", "api_key": "synthetic-secret"}},
        ), "admin")


@pytest.mark.parametrize("template, accepted", [("Actual \x00 NUL", False), (r"Literal \u0000", True)])
def test_artifact_text_checks_distinguish_actual_nul_from_literal_escape(template, accepted):
    _store, service = bank()
    if accepted:
        assert service.create_draft("payments", "review", draft(template=template), "admin").definition["template"] == template
    else:
        with pytest.raises(CapabilityError, match="PostgreSQL text"):
            service.create_draft("payments", "review", draft(template=template), "admin")


def test_project_namespace_cannot_resolve_another_projects_capability():
    store, _service = bank()
    with pytest.raises(CapabilityNotFound):
        store.capabilities.get("other-project", "review")
    assert store.capabilities.list("other-project") == []


def test_dependencies_must_be_published_and_fixed():
    store, service = bank()
    service.create("payments", CapabilitySpec(key="policy", kind="prompt", name="Policy"), "admin")
    service.create_draft("payments", "policy", draft(), "admin")
    service.create_draft("payments", "review", draft(dependencies=[{"key": "policy", "version": "1.0.0"}]), "admin")
    with pytest.raises(CapabilityConflict, match="published"):
        service.publish("payments", "review", "1.0.0", "admin")
    assert store.capabilities.get_version("payments", "review", "1.0.0").status == "draft"
    service.publish("payments", "policy", "1.0.0", "admin")
    service.publish("payments", "review", "1.0.0", "admin")
    assert store.capabilities.get_version("payments", "review", "1.0.0").dependencies[0].version == "1.0.0"


def test_concurrent_publication_is_idempotent_and_audited_once():
    store, service = bank()
    service.create_draft("payments", "review", draft(), "admin")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda actor: service.publish("payments", "review", "1.0.0", actor), ["one", "two"]))
    assert results[0] == results[1]
    events = store.logs.list_by_component("capability_audit")
    assert sum(event.event_type == "capability_published" for event in events) == 1


def api_client(keys):
    app = FastAPI()
    app.state.store = make_store()
    app.state.store.governance.create_project(ProjectCreate(key="payments", name="Payments"), "admin")
    for index, key in enumerate(keys):
        app.state.store.api_key_configs.save(ApiKeyConfig(
            name=key.get("name", f"test-{index}"), api_key="sha256:" + hashlib.sha256(key["key"].encode()).hexdigest(),
            scopes=key["scopes"],
        ))
    app.state.control_plane_auth = ControlPlaneAuth(app.state.store.api_key_configs, app.state.store.governance)
    app.state.rate_limiter = SimpleNamespace(settings={"enabled": False})
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("keys, headers, expected", [
    ([], {}, 401),
    ([{"key": "read", "scopes": ["runs"]}], {"x-api-key": "read"}, 403),
    ([{"key": "admin", "scopes": ["admin"]}], {"x-api-key": "wrong"}, 401),
])
def test_control_plane_rejects_missing_or_unauthorized_credentials(keys, headers, expected):
    client = api_client(keys)
    assert client.get("/v2/projects/payments/capabilities", headers=headers).status_code == expected


def test_prompt_bank_http_lifecycle():
    client = api_client([{"key": "admin-test", "name": "operator", "scopes": ["admin"]}])
    headers = {"x-api-key": "admin-test"}
    base = "/v2/projects/payments/capabilities"
    assert client.post(base, headers=headers, json={"key": "review", "kind": "prompt", "name": "Review"}).status_code == 201
    version_path = f"{base}/review/versions"
    assert client.post(version_path, headers=headers, json=draft().model_dump()).status_code == 201
    assert client.post(f"{version_path}/1.0.0/publish", headers=headers).status_code == 200
    rendered = client.post("/v2/projects/payments/prompts/review/versions/1.0.0/render",
                           headers=headers, json={"variables": {"name": "Synthetic"}})
    assert rendered.status_code == 200
    assert rendered.json()["text"] == "Review Synthetic. Return JSON."
    assert client.post(version_path, headers=headers, json=draft().model_dump()).status_code == 409
    assert client.get(f"{base}/missing", headers=headers).status_code == 404
