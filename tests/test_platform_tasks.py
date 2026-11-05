"""Durable new Task admission through real HTTP and isolated PostgreSQL."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from conftest import fresh_database_url
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_platform_migrations import config_for

from domain import ApiKeyConfig
from domain.capability import CapabilitySpec, CapabilityVersionDraft
from domain.governance import AccessKeyCreate, ProjectCreate, ResourceGrantCreate, ServiceIdentityCreate
from domain.task import TaskConflict, TaskSubmit
from infra.platform_store import PlatformStore
from infra.store.model import AiTask
from infra.store.repository.task import TaskRepository
from interfaces.http.platform_server import create_platform_app
from orchestration.capability_service import CapabilityService
from orchestration.control_plane_auth import ControlPlaneAuth
from orchestration.governance_service import GovernanceService
from orchestration.task_service import TaskService

ADMIN_TOKEN = "synthetic-task-admin"
ADMIN = {"x-api-key": ADMIN_TOKEN}
BASE = "/v2/projects/payments/tasks"


@pytest.fixture
def inbox(monkeypatch, pg_url):
    from interfaces.http import platform_server

    url = fresh_database_url(pg_url)
    command.upgrade(config_for(url), "head")
    store = PlatformStore(create_engine(url))
    with store.engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    store.api_key_configs.save(ApiKeyConfig(
        name="task-admin", api_key="sha256:" + hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest(), scopes=["admin"],
    ))
    for key in ("payments", "other"):
        store.governance.create_project(ProjectCreate(key=key, name=key), "task-admin")
    publish(store)
    monkeypatch.setattr(platform_server, "load_settings", lambda _path: {"database": {"schema": schema}})
    monkeypatch.setattr(platform_server, "build_engine", lambda _settings: create_engine(url))
    try:
        with TestClient(create_platform_app("unused")) as client:
            client.app.state.rate_limiter.settings["enabled"] = False
            yield client, store
    finally:
        store.close()


def publish(store, key="review", version="1", project="payments", *, kind="agent", published=True):
    service = CapabilityService(store.capabilities)
    if version == "1":
        service.create(project, CapabilitySpec(key=key, kind=kind, name=key), "task-admin")
    dependencies = []
    if kind == "agent":
        # Admission tests use the same governed Agent contract as real execution.
        for resource, resource_kind, body in (
            ("fixture-instructions", "prompt", {"template": "Return a JSON object."}),
            ("fixture-policy", "model_policy", {"interaction": "agent_turn", "provider_key": "unconfigured",
                                                "model": "fixture/model", "max_tokens": 128}),
        ):
            if not any(item["key"] == resource for item in store.capabilities.list(project)):
                service.create(project, CapabilitySpec(key=resource, kind=resource_kind, name=resource), "task-admin")
                service.create_draft(project, resource, CapabilityVersionDraft(version="1", definition=body), "task-admin")
                service.publish(project, resource, "1", "task-admin")
            dependencies.append({"key": resource, "version": "1"})
        definition = {"definition_version": "agent.v1", "runtime": "pi",
                      "instruction_prompt": dependencies[0], "model_policy": dependencies[1],
                      "limits": {"max_turns": 1, "max_tool_calls": 0, "timeout_seconds": 60.0}}
    else:
        definition = {"template": "Synthetic ${name}"}
    service.create_draft(project, key, CapabilityVersionDraft(
        version=version, definition=definition, dependencies=dependencies,
        input_schema={"type": "object", "properties": {"name": {"type": "string"}},
                      "required": ["name"], "additionalProperties": False},
    ), "task-admin")
    if published:
        service.publish(project, key, version, "task-admin")


def submit(client, key="request-1", headers=None, **changes):
    return client.post(BASE, headers={**(ADMIN if headers is None else headers), "Idempotency-Key": key},
                       json={"capability_key": "review", "version": "1", "input": {"name": "Synthetic customer"}, **changes})


def test_business_and_customer_associations_are_durable_and_idempotent(inbox):
    client, _store = inbox
    accepted = submit(client, business_id="case-123", customer_id="customer-456")
    assert accepted.status_code == 202
    assert accepted.json()["business_id"] == "case-123"
    assert accepted.json()["customer_id"] == "customer-456"
    assert client.get(accepted.headers["Location"], headers=ADMIN).json()["customer_id"] == "customer-456"
    listed = client.get(BASE, headers=ADMIN, params={"customer_id": "customer-456"})
    assert [item["id"] for item in listed.json()] == [accepted.json()["id"]]
    assert client.get(BASE, headers=ADMIN, params={"customer_id": "other"}).json() == []
    assert submit(client, business_id="case-123", customer_id="customer-456").status_code == 200
    assert submit(client, business_id="case-123", customer_id="different").status_code == 409


def service_identity(store, key="business", actions=("task:submit", "task:read", "task:cancel"), resource="*", project="payments"):
    identity = store.governance.create_identity(project, ServiceIdentityCreate(key=key, name=key), "task-admin")
    issued = GovernanceService(store.governance).issue_key(project, identity["id"], AccessKeyCreate(name="test"), "task-admin")
    for action in actions:
        store.governance.create_grant(project, identity["id"], ResourceGrantCreate(
            action=action, resource_key=resource), "task-admin")
    return identity, issued, {"x-api-key": issued["token"]}


def test_admission_is_durable_pinned_and_never_claims_execution(inbox):
    client, store = inbox
    response = submit(client)
    assert response.status_code == 202
    record = response.json()
    assert response.headers["Location"] == BASE + "/" + record["id"]
    assert response.headers["Cache-Control"] == "no-store"
    assert record["status"] == "queued" and record["finish_time"] is None
    assert record["version"] == "1" and record["capability_kind"] == "agent"
    assert record["submitted_by"] == "admin:task-admin"
    assert not {"input", "request_fingerprint", "idempotency_digest", "result", "worker_id"} & record.keys()
    publish(store, version="2")
    store.capabilities.set_default("payments", "review", "2", "task-admin")
    with Session(store.engine) as session:
        persisted = session.get(AiTask, record["id"])
        assert persisted.input == {"name": "Synthetic customer"}
        assert persisted.idempotency_digest == hashlib.sha256(b"request-1").hexdigest()
        assert persisted.version == "1" and persisted.content_hash == record["content_hash"]
    with TestClient(create_platform_app("unused")) as restarted:
        assert restarted.get(response.headers["Location"], headers=ADMIN).json() == record
    assert "ai_agent_run" not in inspect(store.engine).get_table_names()
    audit = store.logs.list_by_component("task_audit")
    assert len(audit) == 1 and audit[0].event_type == "task_submitted"
    assert "Synthetic customer" not in json.dumps(audit[0].data)


def test_idempotent_replay_conflict_and_cancel_do_not_resubmit(inbox):
    client, store = inbox
    first = submit(client)
    assert submit(client).status_code == 200
    assert submit(client).json() == first.json()
    for changes in ({"input": {"name": "different"}}, {"version": "2"}, {"capability_key": "other"}):
        assert submit(client, **changes).status_code == 409
    target = first.headers["Location"]
    cancelled = client.post(target + "/cancel", headers=ADMIN)
    assert cancelled.status_code == 200 and cancelled.json()["task"]["status"] == "cancelled"
    assert cancelled.json()["task"]["finish_time"] is not None
    assert client.post(target + "/cancel", headers=ADMIN).json() == cancelled.json()
    assert submit(client).json() == cancelled.json()["task"]
    assert [event.event_type for event in store.logs.list_by_component("task_audit")].count("task_cancelled") == 1
    assert submit(client, key="new-key").json()["id"] != first.json()["id"]


def test_concurrent_submission_and_cancel_have_one_record_and_one_event(inbox):
    client, store = inbox
    auth = ControlPlaneAuth(store.api_key_configs, store.governance)
    service = TaskService(store, auth)
    principal = auth.authenticate(ADMIN_TOKEN)
    spec = TaskSubmit(capability_key="review", version="1", input={"name": "Synthetic"})
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: service.submit(principal, "payments", spec, "parallel"), range(4)))
    assert len({record.id for record, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    task_id = results[0][0].id
    with ThreadPoolExecutor(max_workers=3) as executor:
        cancelled = list(executor.map(lambda _: service.cancel(principal, "payments", task_id), range(3)))
    assert all(record == cancelled[0] for record in cancelled)
    events = store.logs.list_by_component("task_audit")
    assert sorted(event.event_type for event in events) == ["task_cancel_requested", "task_cancelled", "task_submitted"]
    assert client.get(BASE, headers=ADMIN).json()[0]["status"] == "cancelled"


def test_scope_is_project_identity_and_capability_not_bearer_key(inbox):
    client, store = inbox
    first_identity, first_key, headers = service_identity(store)
    other_identity, _, other_headers = service_identity(store, key="other-service")
    first = submit(client, headers=headers)
    second = submit(client, headers=other_headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]
    assert client.get(first.headers["Location"], headers=other_headers).status_code == 404
    assert client.post(first.headers["Location"] + "/cancel", headers=other_headers).status_code == 404
    assert len(client.get(BASE, headers=headers).json()) == 1
    rotated = GovernanceService(store.governance).issue_key("payments", first_identity["id"], AccessKeyCreate(name="rotated"), "task-admin")
    store.governance.revoke_key("payments", first_identity["id"], first_key["id"], "task-admin")
    assert client.get(first.headers["Location"], headers=headers).status_code == 401
    rotated_headers = {"x-api-key": rotated["token"]}
    assert submit(client, headers=rotated_headers).json() == first.json()
    assert client.get(first.headers["Location"], headers=ADMIN).status_code == 200
    cross = "/v2/projects/other/tasks/" + first.json()["id"]
    assert client.get(cross, headers=rotated_headers).status_code == 403
    assert client.get(cross, headers=ADMIN).status_code == 404
    assert other_identity["id"] != first_identity["id"]


def test_permissions_are_independent_and_filtered_before_limit(inbox):
    client, store = inbox
    identity, _, headers = service_identity(store, actions=("task:submit",))
    visible = submit(client, headers=headers)
    assert client.get(visible.headers["Location"], headers=headers).status_code == 403
    assert client.post(visible.headers["Location"] + "/cancel", headers=headers).status_code == 403
    assert client.get("/v2/projects/payments/capabilities/review", headers=headers).status_code == 403
    publish(store, "hidden")
    hidden = submit(client, key="hidden", headers=headers, capability_key="hidden")
    assert hidden.status_code == 202
    store.governance.create_grant("payments", identity["id"], ResourceGrantCreate(
        action="task:read", resource_key="review"), "task-admin")
    assert [item["id"] for item in client.get(BASE + "?limit=1", headers=headers).json()] == [visible.json()["id"]]
    assert client.get(hidden.headers["Location"], headers=headers).status_code == 404
    for grant in store.governance.list_grants("payments", identity["id"]):
        store.governance.delete_grant("payments", identity["id"], grant["id"], "task-admin")
    assert client.get(BASE, headers=headers).status_code == 403
    assert submit(client, headers=headers).status_code == 403


@pytest.mark.parametrize("case", ["draft", "prompt", "missing", "schema", "tampered"])
def test_invalid_targets_and_input_create_neither_tasks_nor_audit(inbox, case):
    client, store = inbox
    changes = {}
    expected = 422
    if case in {"draft", "prompt"}:
        publish(store, case, kind="prompt" if case == "prompt" else "agent", published=case != "draft")
        changes["capability_key"] = case
        expected = 409 if case == "draft" else 422
    elif case == "missing":
        changes["version"] = "missing"
        expected = 404
    elif case == "schema":
        changes["input"] = {"name": 123}
    else:
        with store.engine.begin() as connection:
            connection.execute(text("UPDATE ai_capability_version SET content_hash = :hash"), {"hash": "0" * 64})
        expected = 409
    response = submit(client, **changes)
    assert response.status_code == expected
    assert "Synthetic customer" not in response.text
    with Session(store.engine) as session:
        assert session.scalar(select(func.count()).select_from(AiTask)) == 0
    assert store.logs.list_by_component("task_audit") == []


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": "key"}, {**ADMIN, "Idempotency-Key": "bad key"}, ADMIN])
def test_authentication_and_idempotency_header_are_required(inbox, headers):
    client, _ = inbox
    response = client.post(BASE, headers=headers, json={"capability_key": "review", "version": "1"})
    assert response.status_code in {401, 422}


def test_audit_failure_rolls_back_submit_and_cancel(inbox, monkeypatch):
    client, store = inbox
    accepted = submit(client)

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic-private-error")

    monkeypatch.setattr(TaskRepository, "_audit", staticmethod(fail))
    with TestClient(client.app, raise_server_exceptions=False) as safe_client:
        for path, body, extra in [(BASE, {"capability_key": "review", "version": "1", "input": {"name": "secret-input"}},
                                   {"Idempotency-Key": "rollback"}),
                                  (accepted.headers["Location"] + "/cancel", None, {})]:
            response = safe_client.post(path, headers={**ADMIN, **extra}, json=body)
            assert response.status_code == 500
            assert "synthetic-private-error" not in response.text and "secret-input" not in response.text
    with Session(store.engine) as session:
        assert session.scalar(select(func.count()).select_from(AiTask)) == 1
        assert session.get(AiTask, accepted.json()["id"]).status == "queued"


def test_database_rejects_cross_project_task_assignment(inbox):
    client, store = inbox
    accepted = submit(client)
    with store.engine.begin() as connection:
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("UPDATE ai_task SET project_key = 'other' WHERE id = :id"),
                                   {"id": accepted.json()["id"]})


def test_workflow_is_admitted_but_prompt_is_not_a_task(inbox):
    client, store = inbox
    service = CapabilityService(store.capabilities)
    service.create("payments", CapabilitySpec(key="review-flow", kind="workflow", name="Review flow"), "task-admin")
    service.create_draft("payments", "review-flow", CapabilityVersionDraft(
        version="1", definition={"nodes": [{"key": "review", "kind": "rule", "rule": {"operation": "project"}}]},
    ), "task-admin")
    service.publish("payments", "review-flow", "1", "task-admin")
    response = submit(client, capability_key="review-flow")
    assert response.status_code == 202
    assert response.json()["capability_kind"] == "workflow" and response.json()["status"] == "queued"


def test_capability_read_does_not_authorize_task_submission_and_cancel_does_not_require_read(inbox):
    client, store = inbox
    identity, _, headers = service_identity(store, actions=("capability:read",))
    assert client.get("/v2/projects/payments/capabilities/review", headers=headers).status_code == 200
    assert submit(client, headers=headers).status_code == 403
    for action in ("task:submit", "task:cancel"):
        store.governance.create_grant("payments", identity["id"], ResourceGrantCreate(
            action=action, resource_key="review"), "task-admin")
    response = submit(client, headers=headers)
    assert response.status_code == 202
    assert client.get(response.headers["Location"], headers=headers).status_code == 403
    assert client.post(response.headers["Location"] + "/cancel", headers=headers).json()["task"]["status"] == "cancelled"


def test_same_administrator_key_is_independently_idempotent_in_each_project(inbox):
    client, store = inbox
    publish(store, project="other")
    first = submit(client)
    second = client.post("/v2/projects/other/tasks", headers={**ADMIN, "Idempotency-Key": "request-1"},
                         json={"capability_key": "review", "version": "1", "input": {"name": "Synthetic customer"}})
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.parametrize("suspend", ["project", "identity"])
def test_suspension_blocks_existing_task_queries_and_replay_without_removing_tasks(inbox, suspend):
    client, store = inbox
    identity, _, headers = service_identity(store)
    response = submit(client, headers=headers)
    if suspend == "project":
        store.governance.set_project_enabled("payments", False, "task-admin")
    else:
        store.governance.set_identity_enabled("payments", identity["id"], False, "task-admin")
    assert submit(client, headers=headers).status_code == 401
    assert client.get(response.headers["Location"], headers=headers).status_code == 401
    assert client.post(response.headers["Location"] + "/cancel", headers=headers).status_code == 401
    with Session(store.engine) as session:
        assert session.get(AiTask, response.json()["id"]).status == "queued"


def test_invalid_input_is_not_echoed_logged_or_persisted(inbox, caplog):
    client, store = inbox
    for value in ({"name": "nul\x00private"}, {"name": "secret-private", "extra": 1}, {"name": "x" * 1_000_001}):
        response = submit(client, input=value)
        assert response.status_code == 422
        assert "private" not in response.text and "private" not in caplog.text
    assert store.logs.list_by_component("task_audit") == []
    assert client.get(BASE, headers=ADMIN).json() == []


def test_database_rejects_fabricated_success_and_inconsistent_completion_times(inbox):
    client, store = inbox
    accepted = submit(client)
    with store.engine.begin() as connection:
        for mutation in ("status = 'succeeded'", "status = 'cancelled'", "finish_time = now()"):
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(text(f"UPDATE ai_task SET {mutation} WHERE id = :id"),
                                       {"id": accepted.json()["id"]})


def test_concurrent_conflicting_submissions_do_not_overwrite_the_winner(inbox):
    _, store = inbox
    auth = ControlPlaneAuth(store.api_key_configs, store.governance)
    service = TaskService(store, auth)
    principal = auth.authenticate(ADMIN_TOKEN)

    def attempt(name):
        try:
            record, created = service.submit(principal, "payments", TaskSubmit(
                capability_key="review", version="1", input={"name": name}), "conflicting")
            assert created
            return record.id
        except TaskConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ("one", "two")))
    assert outcomes.count("conflict") == 1
    with Session(store.engine) as session:
        assert session.scalar(select(func.count()).select_from(AiTask)) == 1
    assert len(store.logs.list_by_component("task_audit")) == 1


def test_metadata_queries_and_replay_never_load_customer_input(inbox):
    client, _ = inbox
    accepted = submit(client)
    statements = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and "FROM ai_task" in statement:
            statements.append(statement)

    engine = client.app.state.store.engine
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        assert client.get(BASE, headers=ADMIN).status_code == 200
        assert client.get(accepted.headers["Location"], headers=ADMIN).status_code == 200
        assert submit(client).status_code == 200
        assert client.post(accepted.headers["Location"] + "/cancel", headers=ADMIN).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    assert len(statements) == 4
    assert all("ai_task.input" not in statement for statement in statements)
