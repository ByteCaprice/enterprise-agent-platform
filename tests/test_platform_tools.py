"""Real HTTP -> read-only tool Workflow -> fenced call evidence, using isolated SIT."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_platform_tasks import ADMIN, service_identity, submit
from test_platform_tasks import inbox as inbox_fixture
from worker_support import tool_gateway

from domain.capability import CapabilityDependency, CapabilitySpec, CapabilityVersionDraft
from domain.capability_definitions import WORKFLOW_CHECKPOINT_PROFILE, WORKFLOW_INTERPRETER_PROFILE
from domain.platform_tool import InstalledReadonlyTool
from domain.task_execution import ExecutionFailure
from framework.readonly_tools import ReadonlyToolRegistry, builtin_readonly_tools
from infra.schema_validation import IsolatedJSONValidator
from interfaces.cli.platform import main
from interfaces.worker.platform import run_workflow_worker
from orchestration.capability_service import CapabilityService

inbox = inbox_fixture
BINDINGS = "/v2/projects/payments/tool-bindings"


def source(node, pointer):
    return {"source": "node", "node": node, "pointer": pointer}


def setup_tool(client, store, *, handler="json.project", version="1", timeout=30, missing_binding=False):
    response = client.post(BINDINGS, headers=ADMIN, json={
        "key": "projection", "name": "Approved read-only projection", "handler_key": handler,
        "handler_version": version, "operation_type": "read_only", "max_timeout_seconds": 30,
    })
    assert response.status_code == 201
    service = CapabilityService(store.capabilities, IsolatedJSONValidator())
    service.create("payments", CapabilitySpec(key="select-fields", kind="tool", name="Select fields"), "task-admin")
    schema = {"type": "object", "properties": {"name": {"type": "string"}},
              "required": ["name"], "additionalProperties": False}
    service.create_draft("payments", "select-fields", CapabilityVersionDraft(
        version="1", definition={"binding_key": "missing" if missing_binding else "projection",
                                  "operation_type": "read_only", "timeout_seconds": timeout},
        input_schema={"type": "object", "required": ["data", "fields"], "additionalProperties": False,
                      "properties": {"data": {"type": "object"}, "fields": {"type": "array", "items": {"type": "string"}}}},
        output_schema=schema,
    ), "task-admin")
    service.publish("payments", "select-fields", "1", "task-admin")
    service.create("payments", CapabilitySpec(key="tool-flow", kind="workflow", name="Tool flow"), "task-admin")
    service.create_draft("payments", "tool-flow", CapabilityVersionDraft(
        version="1", definition={"nodes": [
            {"key": "prepare", "kind": "rule", "rule": {"operation": "project"}, "input_mapping": {
                "data": {"source": "input", "pointer": "/data"}, "fields": {"source": "literal", "value": ["name"]}}},
            {"key": "select", "kind": "tool", "capability": {"key": "select-fields", "version": "1"},
             "depends_on": ["prepare"], "input_mapping": {"data": source("prepare", "/data"), "fields": source("prepare", "/fields")}},
            {"key": "finish", "kind": "rule", "rule": {"operation": "project"}, "depends_on": ["select"],
             "input_mapping": {"name": source("select", "/name")}},
        ], "output_mapping": {"name": source("finish", "/name")}},
        dependencies=[{"key": "select-fields", "version": "1"}], output_schema=schema,
    ), "task-admin")
    service.publish("payments", "tool-flow", "1", "task-admin")


def accept(client, headers=None):
    response = submit(client, headers=headers, capability_key="tool-flow",
                      input={"data": {"name": "Synthetic visible name", "private": "synthetic-private-input"}})
    assert response.status_code == 202
    return response.headers["Location"]


def grant(client, identity):
    response = client.post(f"/v2/projects/payments/service-identities/{identity['id']}/grants", headers=ADMIN,
                           json={"action": "tool:invoke", "resource_key": "select-fields"})
    assert response.status_code == 201
    return response.json()


def execute(store, handler=None):
    registry = ReadonlyToolRegistry([InstalledReadonlyTool("test.handler", "1", handler)]) if handler else builtin_readonly_tools()
    return asyncio.run(run_workflow_worker(store, once=True, tool_gateway=tool_gateway(store, registry)))


def test_cli_builtin_tool_executes_real_projection_with_scoped_metadata_and_immutable_binding(inbox, capsys):
    client, store = inbox
    setup_tool(client, store)
    identity, _, headers = service_identity(store)
    grant(client, identity)
    agent = submit(client, key="agent-left-queued").headers["Location"]
    target = accept(client, headers)
    main(["worker", "--once", "--config-dir", "unused"])
    assert '"claimed": true' in capsys.readouterr().out
    assert client.get(agent, headers=ADMIN).json()["status"] == "queued"
    assert client.get(target + "/result", headers=headers).json()["result"] == {"name": "Synthetic visible name"}
    assert [row["status"] for row in client.get(target + "/nodes", headers=headers).json()] == ["succeeded"] * 3
    response = client.get(target + "/tool-calls", headers=headers)
    record = response.json()[0]
    assert response.headers["Cache-Control"] == "no-store"
    assert record["status"] == "succeeded" and record["handler_key"] == "json.project" and record["handler_version"] == "1"
    assert record["tool_version"] == "1" and record["operation_type"] == "read_only" and record["output_hash"]
    assert not {"input", "output", "token", "arguments", "definition"} & record.keys()
    assert "synthetic-private-input" not in response.text
    _, _, other = service_identity(store, key="other-system")
    assert client.get(target + "/tool-calls", headers=other).status_code == 404
    assert client.get(BINDINGS, headers=headers).status_code == 403
    assert client.get("/v2/projects/other/tool-bindings/projection", headers=ADMIN).status_code == 404
    for payload in ({"handler_version": "2"}, {"command": "sh"}, {"enabled": "false"}):
        assert client.patch(BINDINGS + "/projection", headers=ADMIN, json=payload).status_code == 422


@pytest.mark.parametrize("case,expected", [
    ("grant", "TOOL_ACCESS_DENIED"), ("disabled", "TOOL_BINDING_DISABLED"),
    ("handler", "TOOL_HANDLER_UNAVAILABLE"), ("binding", "TOOL_CONFIGURATION_UNAVAILABLE"),
])
def test_missing_authority_or_binding_never_invokes_handler(inbox, case, expected):
    client, store = inbox
    setup_tool(client, store, version="uninstalled" if case == "handler" else "1", missing_binding=case == "binding")
    identity, _, headers = service_identity(store)
    if case != "grant":
        grant(client, identity)
    if case == "disabled":
        assert client.patch(BINDINGS + "/projection", headers=ADMIN, json={"enabled": False}).status_code == 200
    target = accept(client, headers)
    assert execute(store)
    result = client.get(target + "/result", headers=ADMIN).json()
    assert result["status"] == "failed" and result["error_code"] == expected
    assert client.get(target + "/tool-calls", headers=ADMIN).json() == []


@pytest.mark.parametrize("invalid_output", [True, False])
def test_handler_output_and_errors_are_sanitized_without_replay(inbox, invalid_output):
    client, store = inbox
    setup_tool(client, store, handler="test.handler")
    seen = []

    async def handler(context, _values):
        assert context.project_key == "payments" and context.tool_key == "select-fields"
        assert not hasattr(context, "token")
        seen.append(True)
        if invalid_output:
            return {"unexpected": "synthetic-private-output"}
        raise RuntimeError("synthetic-private-exception")

    target = accept(client)
    assert execute(store, handler) and len(seen) == 1
    result = client.get(target + "/result", headers=ADMIN)
    expected = "TOOL_OUTPUT_INVALID" if invalid_output else "TOOL_EXECUTION_FAILED_RESULT_UNKNOWN"
    assert result.json()["error_code"] == expected and result.json()["result"] is None
    assert "synthetic-private" not in result.text
    calls = client.get(target + "/tool-calls", headers=ADMIN)
    assert calls.json()[0]["error_code"] == expected and "synthetic-private" not in calls.text
    assert not execute(store, handler)


def test_grant_revocation_after_handler_returns_blocks_result_delivery(inbox):
    client, store = inbox
    setup_tool(client, store, handler="test.handler")
    identity, _, headers = service_identity(store)
    permission = grant(client, identity)

    async def handler(context, values):
        assert context.submitted_by == "service:" + identity["id"]
        await asyncio.to_thread(store.governance.delete_grant, "payments", identity["id"], permission["id"], "task-admin")
        return {"name": values["data"]["name"]}

    target = accept(client, headers)
    assert execute(store, handler)
    result = client.get(target + "/result", headers=headers).json()
    assert result["status"] == "failed" and result["error_code"] == "TOOL_ACCESS_DENIED" and result["result"] is None
    assert client.get(target + "/tool-calls", headers=headers).json()[0]["status"] == "failed"


def test_handler_suppressing_its_timeout_cannot_deliver_late_success(inbox):
    client, store = inbox
    setup_tool(client, store, handler="test.handler", timeout=0.02)
    seen = []

    async def handler(_context, _values):
        seen.append(True)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return {"name": "late-result"}

    target = accept(client)
    assert execute(store, handler) and len(seen) == 1
    result = client.get(target + "/result", headers=ADMIN).json()
    assert result["status"] == "timed_out" and result["error_code"] == "TOOL_TIMEOUT_RESULT_UNKNOWN"
    assert client.get(target + "/tool-calls", headers=ADMIN).json()[0]["status"] == "failed"


def test_expired_tool_reply_is_fenced_and_recovery_finalizes_pending_evidence(inbox):
    client, store = inbox
    setup_tool(client, store, handler="test.handler")
    target = accept(client)
    task_id = target.rsplit("/", 1)[1]

    def expire():
        with store.engine.begin() as connection:
            connection.execute(text("UPDATE ai_task_attempt SET lease_expires_at=start_time WHERE task_id=:id"), {"id": task_id})

    async def handler(_context, values):
        await asyncio.to_thread(expire)
        return {"name": values["data"]["name"]}

    assert execute(store, handler)
    assert client.get(target, headers=ADMIN).json()["status"] == "running"
    assert store.task_queue.recover_expired() == 1
    call = client.get(target + "/tool-calls", headers=ADMIN).json()[0]
    assert call["status"] == "failed" and call["error_code"] == "TOOL_LEASE_EXPIRED_RESULT_UNKNOWN"
    assert call["output_hash"] is None
    assert client.get(target + "/result", headers=ADMIN).json()["result"] is None


def test_tool_call_cannot_be_replayed_and_null_success_hash_is_rejected(inbox):
    client, store = inbox
    setup_tool(client, store)
    accept(client)
    from worker_support import worker_capabilities

    claim = store.task_queue.claim("tool-ledger-worker", capabilities=worker_capabilities("workflow"), lease_seconds=60)
    checkpoint = store.workflows.begin(claim.lease, "select", "a" * 64,
                                       interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
                                       checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE)
    gateway = tool_gateway(store, builtin_readonly_tools())
    reference = CapabilityDependency(key="select-fields", version="1")
    values = {"data": {"name": "Synthetic"}, "fields": ["name"]}
    assert asyncio.run(gateway.invoke(claim, checkpoint.record.id, reference, values)) == {"name": "Synthetic"}
    with pytest.raises(ExecutionFailure, match="TOOL_CALL_CONFLICT"):
        asyncio.run(gateway.invoke(claim, checkpoint.record.id, reference, values))
    with store.engine.begin() as connection:
        for mutation in ("output_hash=NULL", "status='failed', output_hash=NULL, error_code=NULL",
                         "finish_time=start_time - interval '1 second'"):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text(f"UPDATE ai_platform_tool_call SET {mutation}"))


def test_call_audit_failure_rolls_back_evidence_and_never_runs_handler(inbox, monkeypatch):
    client, store = inbox
    setup_tool(client, store, handler="test.handler")
    seen = []

    async def handler(_context, _values):
        seen.append(True)
        return {"name": "must-not-run"}

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic-private-audit-error")

    monkeypatch.setattr("infra.store.repository.tools.record_tool_call_transition", fail)
    target = accept(client)
    assert execute(store, handler) and not seen
    result = client.get(target + "/result", headers=ADMIN)
    assert result.json()["error_code"] == "TOOL_ACCOUNTING_UNAVAILABLE" and "private" not in result.text
    assert client.get(target + "/tool-calls", headers=ADMIN).json() == []


def test_finish_audit_failure_preserves_unknown_outcome_instead_of_delivering_success(inbox, monkeypatch):
    client, store = inbox
    setup_tool(client, store)
    from infra.store.repository import tools

    real_transition = tools.record_tool_call_transition

    def fail_finish(session, project, actor, event, **kwargs):
        if event == "tool_call_succeeded":
            raise RuntimeError("synthetic-private-finish-error")
        return real_transition(session, project, actor, event, **kwargs)

    monkeypatch.setattr(tools, "record_tool_call_transition", fail_finish)
    target = accept(client)
    assert execute(store)
    result = client.get(target + "/result", headers=ADMIN).json()
    assert result["status"] == "failed" and result["result"] is None
    assert result["error_code"] == "TOOL_ACCOUNTING_RESULT_UNKNOWN"
    record = client.get(target + "/tool-calls", headers=ADMIN).json()[0]
    assert record["status"] == "failed" and record["output_hash"] is None
