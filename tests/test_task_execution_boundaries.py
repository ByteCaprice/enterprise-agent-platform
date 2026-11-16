"""Defense-in-depth checks at the new Task persistence boundary."""

from __future__ import annotations

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from test_platform_tasks import ADMIN, BASE, publish_completion_flow, record_completion_node, submit
from test_platform_tasks import inbox as inbox_fixture

from domain.capability import CapabilityConflict
from domain.schema_validation import JSONValidationError

inbox = inbox_fixture


def publish_output(store, schema):
    publish_completion_flow(store, version="2", output_schema=schema)


def test_pinned_output_schema_is_enforced_before_atomic_completion(inbox):
    client, store = inbox
    publish_output(store, {"type": "object", "properties": {"approved": {"type": "boolean"}},
                           "required": ["approved"], "additionalProperties": False})
    target = submit(client, capability_key="completion-flow", version="2").headers["Location"]
    from worker_support import worker_capabilities

    claim = store.task_queue.claim("schema-worker", capabilities=worker_capabilities())
    with pytest.raises(JSONValidationError, match="INSTANCE_INVALID"):
        store.task_completion.complete(claim.lease, status="succeeded", result={"approved": "yes"})
    assert client.get(target, headers=ADMIN).json()["status"] == "running"
    record_completion_node(store, claim, {"approved": True})
    assert store.task_completion.complete(claim.lease, status="succeeded", result={"approved": True})


def test_root_local_schema_reference_is_supported_without_network(inbox):
    client, store = inbox
    publish_output(store, {"type": "object", "properties": {"child": {"$ref": "#"}}})
    submit(client, capability_key="completion-flow", version="2")
    from worker_support import worker_capabilities

    claim = store.task_queue.claim("schema-worker", capabilities=worker_capabilities())
    record_completion_node(store, claim, {"child": {}})
    assert store.task_completion.complete(claim.lease, status="succeeded", result={"child": {}})


@pytest.mark.parametrize("reference", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_completion_never_resolves_external_schema_references(inbox, reference):
    client, store = inbox
    submit(client)
    from worker_support import worker_capabilities

    claim = store.task_queue.claim("schema-worker", capabilities=worker_capabilities())
    # Simulate corruption after claim; the persistence boundary must still reject remote IO.
    with store.engine.begin() as connection:
        connection.execute(text("UPDATE ai_capability_version SET output_schema = CAST(:schema AS jsonb)"),
                           {"schema": '{"' + reference + '": "https://example.invalid/private-schema"}'})
    with pytest.raises(CapabilityConflict, match="content hash"):
        store.task_completion.complete(claim.lease, status="succeeded", result={})


@pytest.mark.parametrize("operation", ["completion", "recovery"])
def test_terminal_audit_failure_rolls_back_task_and_attempt(inbox, monkeypatch, operation):
    client, store = inbox
    publish_completion_flow(store)
    target = submit(client, capability_key="completion-flow").headers["Location"]
    from worker_support import worker_capabilities

    claim = store.task_queue.claim("audit-worker", capabilities=worker_capabilities())
    if operation == "completion":
        record_completion_node(store, claim, {"ok": True})
    if operation == "recovery":
        with store.engine.begin() as connection:
            connection.execute(text("UPDATE ai_task_attempt SET lease_expires_at = start_time"))

    def fail(*_args, **_kwargs):
        raise RuntimeError("private-audit-failure")

    module = "task_completion" if operation == "completion" else "task_queue"
    monkeypatch.setattr(f"infra.store.repository.{module}.record_task_transition", fail)
    with pytest.raises(RuntimeError, match="private-audit-failure"):
        if operation == "completion":
            store.task_completion.complete(claim.lease, status="succeeded", result={"ok": True})
        else:
            store.task_queue.recover_expired()
    assert client.get(target, headers=ADMIN).json()["status"] == "running"
    assert client.get(target + "/attempts", headers=ADMIN).json()[0]["status"] == "running"
    assert client.get(target + "/result", headers=ADMIN).json()["result"] is None


def test_database_checks_reject_null_failure_codes_and_result_state_mismatch(inbox):
    client, store = inbox
    submit(client)
    from worker_support import worker_capabilities

    store.task_queue.claim("constraint-worker", capabilities=worker_capabilities())
    mutations = [
        "UPDATE ai_task SET status='failed', finish_time=clock_timestamp(), error_code=NULL",
        "UPDATE ai_task SET status='succeeded', finish_time=clock_timestamp(), result=NULL",
        "UPDATE ai_task SET result='{}'::jsonb",
        "UPDATE ai_task_attempt SET status='failed', finish_time=clock_timestamp(), error_code=NULL",
        "UPDATE ai_task_attempt SET error_code='PRIVATE_ERROR'",
    ]
    with store.engine.begin() as connection:
        for statement in mutations:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text(statement))


def test_metadata_and_replay_do_not_select_results_and_attempts_do_not_select_tokens(inbox):
    client, store = inbox
    publish_completion_flow(store)
    target = submit(client, capability_key="completion-flow").headers["Location"]
    from worker_support import worker_capabilities

    claim = store.task_queue.claim("privacy-worker", capabilities=worker_capabilities())
    record_completion_node(store, claim, {"private": "synthetic"})
    assert store.task_completion.complete(claim.lease, status="succeeded", result={"private": "synthetic"})
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = client.app.state.store.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert client.get(BASE, headers=ADMIN).status_code == 200
        assert client.get(target, headers=ADMIN).status_code == 200
        assert submit(client, capability_key="completion-flow").status_code == 200
        assert client.get(target + "/attempts", headers=ADMIN).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record)
    task_queries = [statement for statement in statements if "FROM ai_task" in statement]
    assert len(task_queries) == 5
    assert all("ai_task.input" not in statement and "ai_task.result" not in statement for statement in task_queries)
    assert all("ai_task_attempt.token" not in statement for statement in task_queries)
