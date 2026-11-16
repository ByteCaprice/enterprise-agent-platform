"""New-platform authorization is tested against actual PostgreSQL and HTTP."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from conftest import fresh_database_url, make_store
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from domain import ApiKeyConfig, utc_now
from domain.capability import CapabilitySpec, CapabilityVersionDraft
from domain.governance import AccessKeyCreate, ProjectCreate, ResourceGrantCreate, ServiceIdentityCreate
from infra.platform_store import PlatformStore
from infra.schema_validation import IsolatedJSONValidator
from infra.store.model import AiServiceAccessKey
from interfaces.http.platform_server import create_platform_app
from orchestration.capability_service import CapabilityService
from orchestration.control_plane_auth import ControlPlaneAuth
from orchestration.governance_service import GovernanceService

ADMIN_TOKEN = "synthetic-platform-admin-for-tests"
pytestmark = pytest.mark.usefixtures("pg_url")
ADMIN = {"x-api-key": ADMIN_TOKEN}


@pytest.fixture
def platform(monkeypatch, pg_url):
    from interfaces import runtime

    url = fresh_database_url(pg_url)
    engine = create_engine(url)
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "infra/platform_migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
    store = PlatformStore(engine)
    store.api_key_configs.save(ApiKeyConfig(
        name="test-admin", api_key="sha256:" + hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest(), scopes=["admin"],
    ))
    store.governance.create_project(ProjectCreate(key="payments", name="Payments"), "test-admin")
    store.governance.create_project(ProjectCreate(key="other", name="Other"), "test-admin")
    monkeypatch.setattr(runtime, "load_settings", lambda _path: {"database": {"schema": schema}})
    monkeypatch.setattr(runtime, "build_engine", lambda _settings: create_engine(url))
    try:
        with TestClient(create_platform_app("unused")) as client:
            client.app.state.rate_limiter.settings["enabled"] = False
            yield client, store
    finally:
        store.close()


def identity(client, project="payments", key="business"):
    response = client.post(f"/v2/projects/{project}/service-identities", headers=ADMIN,
                           json={"key": key, "name": "Synthetic integration"})
    assert response.status_code == 201
    return response.json()["id"]


def issue(client, identity_id, project="payments", **extra):
    response = client.post(f"/v2/projects/{project}/service-identities/{identity_id}/keys", headers=ADMIN,
                           json={"name": "test key", **extra})
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def grant(client, identity_id, action, resource="*", project="payments"):
    response = client.post(f"/v2/projects/{project}/service-identities/{identity_id}/grants", headers=ADMIN,
                           json={"action": action, "resource_key": resource})
    assert response.status_code == 201
    return response.json()


def prompt(store, key="review", project="payments"):
    service = CapabilityService(store.capabilities, IsolatedJSONValidator())
    service.create(project, CapabilitySpec(key=key, kind="prompt", name=key), "test-admin")
    service.create_draft(project, key, CapabilityVersionDraft(
        version="1", definition={"template": "Synthetic published prompt"},
    ), "test-admin")
    service.publish(project, key, "1", "test-admin")


def test_service_keys_are_digest_only_one_time_and_not_administrators(platform):
    client, store = platform
    identity_id = identity(client)
    issued = issue(client, identity_id)
    base = f"/v2/projects/payments/service-identities/{identity_id}"
    headers = {"x-api-key": issued["token"]}
    with Session(store.engine) as session:
        record = session.get(AiServiceAccessKey, issued["id"])
        assert record.digest == "sha256:" + hashlib.sha256(issued["token"].encode()).hexdigest()
        assert client.get("/v2/projects/payments/capabilities", headers={"x-api-key": record.digest}).status_code == 401
    assert "digest" not in issued
    listed = client.get(base + "/keys", headers=ADMIN).json()
    assert "token" not in listed[0] and "digest" not in listed[0]
    audit = json.dumps([event.data for event in store.logs.list_by_component("governance_audit")])
    assert issued["token"] not in audit and "sha256:" not in audit
    for method, path, body in [
        ("GET", "/v2/projects", None),
        ("POST", "/v2/projects", {"key": "escape", "name": "Escape"}),
        ("GET", base + "/keys", None),
        ("POST", base + "/keys", {"name": "escalate"}),
        ("POST", base + "/grants", {"action": "capability:write", "resource_key": "*"}),
    ]:
        assert client.request(method, path, headers=headers, json=body).status_code == 403
    assert client.get("/v2/projects/payments/capabilities", headers=headers).status_code == 403


def test_resource_filtered_list_and_direct_access_do_not_leak_other_projects(platform):
    client, store = platform
    for key in ("aaa-hidden", "zzz-visible"):
        prompt(store, key)
    prompt(store, "zzz-visible", "other")
    identity_id = identity(client)
    issued = issue(client, identity_id)
    headers = {"x-api-key": issued["token"], "x-project-key": "other", "x-role": "admin"}
    grant(client, identity_id, "capability:read", "zzz-visible")
    base = "/v2/projects/payments/capabilities"
    # Filtering happens in SQL BEFORE limit, not after reading arbitrary rows.
    assert [row["key"] for row in client.get(base + "?limit=1", headers=headers).json()] == ["zzz-visible"]
    assert client.get(base + "/aaa-hidden", headers=headers).status_code == 403
    assert client.get(base + "/aaa-hidden/versions/1", headers=headers).status_code == 403
    assert client.get(base + "/zzz-visible", headers=headers).status_code == 200
    assert client.get("/v2/projects/other/capabilities/zzz-visible", headers=headers).status_code == 403
    grant(client, identity_id, "capability:read")
    assert len(client.get(base, headers=headers).json()) == 2
    assert client.get("/v2/projects/other/capabilities", headers=headers).status_code == 403


def test_write_publish_and_render_are_independent_permissions(platform):
    client, _store = platform
    identity_id = identity(client)
    headers = {"x-api-key": issue(client, identity_id)["token"]}
    grant(client, identity_id, "capability:write")
    base = "/v2/projects/payments/capabilities/review"
    assert client.post("/v2/projects/payments/capabilities", headers=headers,
                       json={"key": "review", "kind": "prompt", "name": "Review"}).status_code == 201
    assert client.post(base + "/versions", headers=headers,
                       json={"version": "1", "definition": {"template": "Synthetic"}}).status_code == 201
    assert client.post(base + "/versions/1/publish", headers=headers).status_code == 403
    grant(client, identity_id, "capability:publish", "review")
    assert client.post(base + "/versions/1/publish", headers=headers).status_code == 200
    assert client.post(base + "/versions/1/set-default", headers=headers).status_code == 200
    render = "/v2/projects/payments/prompts/review/versions/1/render"
    assert client.post(render, headers=headers, json={"variables": {}}).status_code == 403
    grant(client, identity_id, "prompt:render", "review")
    assert client.post(render, headers=headers, json={"variables": {}}).json()["text"] == "Synthetic"
    assert client.get(base, headers=headers).status_code == 403


@pytest.mark.parametrize("revocation", ["grant", "key", "identity", "project", "expiry"])
def test_revocation_is_effective_without_restart_or_identity_cache(platform, revocation):
    client, store = platform
    prompt(store)
    identity_id = identity(client)
    issued = issue(client, identity_id)
    permission = grant(client, identity_id, "capability:read", "review")
    headers = {"x-api-key": issued["token"]}
    target = "/v2/projects/payments/capabilities/review"
    assert client.get(target, headers=headers).status_code == 200
    base = f"/v2/projects/payments/service-identities/{identity_id}"
    if revocation == "grant":
        assert client.delete(base + f"/grants/{permission['id']}", headers=ADMIN).status_code == 204
    elif revocation == "key":
        assert client.post(base + f"/keys/{issued['id']}/revoke", headers=ADMIN).status_code == 200
        assert client.post(base + f"/keys/{issued['id']}/revoke", headers=ADMIN).status_code == 200
        assert sum(event.event_type == "service_key_revoked" for event in
                   store.logs.list_by_component("governance_audit")) == 1
    elif revocation == "identity":
        assert client.patch(base, headers=ADMIN, json={"enabled": False}).status_code == 200
    elif revocation == "project":
        assert client.patch("/v2/projects/payments", headers=ADMIN, json={"enabled": False}).status_code == 200
        # Admin can manage a suspended project, but cannot use its capabilities.
        assert client.get(target, headers=ADMIN).status_code == 403
        assert client.get("/v2/projects/payments", headers=ADMIN).status_code == 200
    else:
        with store.engine.begin() as connection:
            connection.execute(text("UPDATE ai_service_access_key SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
                                    "WHERE id = :id"), {"id": issued["id"]})
    assert client.get(target, headers=headers).status_code == (403 if revocation == "grant" else 401)


def test_rotation_has_independent_irreversible_revocation(platform):
    client, store = platform
    prompt(store)
    identity_id = identity(client)
    first, second = issue(client, identity_id), issue(client, identity_id)
    grant(client, identity_id, "capability:read", "review")
    base = f"/v2/projects/payments/service-identities/{identity_id}"
    assert client.post(base + f"/keys/{first['id']}/revoke", headers=ADMIN).status_code == 200
    for enabled in (False, True):
        assert client.patch(base, headers=ADMIN, json={"enabled": enabled}).status_code == 200
    target = "/v2/projects/payments/capabilities/review"
    assert client.get(target, headers={"x-api-key": first["token"]}).status_code == 401
    assert client.get(target, headers={"x-api-key": second["token"]}).status_code == 200


def test_exact_write_permissions_cannot_create_or_modify_another_capability(platform):
    client, store = platform
    prompt(store, "allowed")
    prompt(store, "hidden")
    identity_id = identity(client)
    headers = {"x-api-key": issue(client, identity_id)["token"]}
    grant(client, identity_id, "capability:write", "allowed")
    draft = {"version": "2", "definition": {"template": "Synthetic"}}
    assert client.post("/v2/projects/payments/capabilities/allowed/versions", headers=headers, json=draft).status_code == 201
    assert client.post("/v2/projects/payments/capabilities/hidden/versions", headers=headers, json=draft).status_code == 403
    assert client.post("/v2/projects/payments/capabilities", headers=headers,
                       json={"key": "new", "kind": "prompt", "name": "New"}).status_code == 403


def test_disabled_project_prevents_key_and_grant_issuance_but_can_be_managed(platform):
    client, _store = platform
    identity_id = identity(client)
    base = f"/v2/projects/payments/service-identities/{identity_id}"
    assert client.patch("/v2/projects/payments", headers=ADMIN, json={"enabled": False}).status_code == 200
    assert client.post(base + "/keys", headers=ADMIN, json={"name": "denied"}).status_code == 403
    assert client.post(base + "/grants", headers=ADMIN,
                       json={"action": "capability:read", "resource_key": "*"}).status_code == 403
    assert client.post("/v2/projects/payments/service-identities", headers=ADMIN,
                       json={"key": "denied", "name": "Denied"}).status_code == 403
    assert client.patch("/v2/projects/payments", headers=ADMIN, json={"enabled": True}).status_code == 200
    assert client.get("/v2/projects/payments/capabilities", headers=ADMIN).status_code == 200


def test_project_and_identity_creation_conflicts_and_foreign_resource_grants(platform):
    client, store = platform
    assert client.post("/v2/projects", headers=ADMIN,
                       json={"key": "payments", "name": "Duplicate"}).status_code == 409
    identity_id = identity(client)
    assert client.post("/v2/projects/payments/service-identities", headers=ADMIN,
                       json={"key": "business", "name": "Duplicate"}).status_code == 409
    assert identity(client, "other", "business") != identity_id
    prompt(store, "foreign", "other")
    assert client.post(f"/v2/projects/payments/service-identities/{identity_id}/grants", headers=ADMIN,
                       json={"action": "capability:read", "resource_key": "foreign"}).status_code == 404
    assert client.post(f"/v2/projects/payments/service-identities/{identity_id}/grants", headers=ADMIN,
                       json={"action": "admin", "resource_key": "*"}).status_code == 422
    assert client.get("/v2/projects/missing/capabilities", headers=ADMIN).status_code == 404


def test_anonymous_and_deleted_last_administrator_never_open_access(platform):
    client, store = platform
    for path in ("/v2/projects", "/v2/projects/payments/service-identities", "/v2/projects/payments/capabilities"):
        assert client.get(path).status_code == 401
    store.api_key_configs.delete("test-admin")
    for headers in ({}, ADMIN, {"x-api-key": "sha256:" + "0" * 64}):
        assert client.get("/v2/projects", headers=headers).status_code == 401
        assert client.get("/v2/projects/payments/capabilities", headers=headers).status_code == 401


def test_cross_project_identity_and_grant_mutations_are_rejected(platform):
    client, store = platform
    prompt(store)
    identity_id = identity(client)
    issued = issue(client, identity_id)
    permission = grant(client, identity_id, "capability:read", "review")
    base = f"/v2/projects/other/service-identities/{identity_id}"
    assert client.patch(base, headers=ADMIN, json={"enabled": False}).status_code == 404
    assert client.post(base + "/keys", headers=ADMIN, json={"name": "wrong project"}).status_code == 404
    assert client.post(base + f"/keys/{issued['id']}/revoke", headers=ADMIN).status_code == 404
    assert client.delete(base + f"/grants/{permission['id']}", headers=ADMIN).status_code == 404
    with store.engine.begin() as connection:
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("INSERT INTO ai_resource_grant "
                                        "(id, project_key, identity_id, action, resource_key, created_by, create_time) "
                                        "VALUES ('invalid', 'other', :id, 'capability:read', '*', 'test', now())"),
                                   {"id": identity_id})


@pytest.mark.parametrize("body", [
    {"name": "expired", "expires_at": "2000-01-01T00:00:00Z"},
    {"name": "naive", "expires_at": "2030-01-01T00:00:00"},
    {"name": "scope injection", "scopes": ["admin"]},
    {"name": "nul\x00"},
])
def test_invalid_key_requests_cannot_expand_authority_or_create_credentials(platform, body):
    client, store = platform
    identity_id = identity(client)
    assert client.post(f"/v2/projects/payments/service-identities/{identity_id}/keys",
                       headers=ADMIN, json=body).status_code == 422
    assert store.governance.list_keys("payments", identity_id) == []


def test_grants_are_serialized_idempotent_and_audited_atomically():
    store = make_store()
    try:
        store.governance.create_project(ProjectCreate(key="payments", name="Payments"), "admin")
        row = store.governance.create_identity("payments", ServiceIdentityCreate(key="system", name="System"), "admin")
        spec = ResourceGrantCreate(action="capability:read", resource_key="*")
        with ThreadPoolExecutor(max_workers=2) as executor:
            grants = list(executor.map(lambda actor: store.governance.create_grant("payments", row["id"], spec, actor),
                                       ["one", "two"]))
        assert grants[0] == grants[1]
        assert sum(event.event_type == "resource_grant_created" for event in
                   store.logs.list_by_component("governance_audit")) == 1
    finally:
        store.close()


def test_audit_failure_rolls_back_identity_and_key_creation(monkeypatch):
    store = make_store()
    try:
        store.governance.create_project(ProjectCreate(key="payments", name="Payments"), "admin")
        row = store.governance.create_identity("payments", ServiceIdentityCreate(key="system", name="System"), "admin")

        def broken_audit(*_args, **_kwargs):
            raise RuntimeError("Synthetic audit failure")

        monkeypatch.setattr(store.governance, "_audit", broken_audit)
        with pytest.raises(RuntimeError, match="audit failure"):
            GovernanceService(store.governance).issue_key("payments", row["id"], AccessKeyCreate(name="test"), "admin")
        assert store.governance.list_keys("payments", row["id"]) == []
        with pytest.raises(RuntimeError, match="audit failure"):
            store.governance.create_identity("payments", ServiceIdentityCreate(key="rollback", name="Rollback"), "admin")
        assert len(store.governance.list_identities("payments")) == 1
    finally:
        store.close()


def test_authorization_rechecks_credentials_even_for_previously_authenticated_principal():
    store = make_store()
    try:
        store.governance.create_project(ProjectCreate(key="payments", name="Payments"), "admin")
        row = store.governance.create_identity("payments", ServiceIdentityCreate(key="system", name="System"), "admin")
        issued = GovernanceService(store.governance).issue_key(
            "payments", row["id"], AccessKeyCreate(name="test", expires_at=utc_now() + timedelta(hours=1)), "admin")
        store.governance.create_grant("payments", row["id"], ResourceGrantCreate(
            action="capability:read", resource_key="*"), "admin")
        auth = ControlPlaneAuth(store.api_key_configs, store.governance)
        principal = auth.authenticate(issued["token"])
        assert auth.allowed_resources(principal, "payments", "capability:read", "review") == {"*"}
        store.governance.revoke_key("payments", row["id"], issued["id"], "admin")
        from domain.governance import AccessDenied
        with pytest.raises(AccessDenied):
            auth.allowed_resources(principal, "payments", "capability:read", "review")
        assert issued["token"] not in repr(principal) and "sha256:" not in repr(principal)
    finally:
        store.close()
