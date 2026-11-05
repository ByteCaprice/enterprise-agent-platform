"""Real HTTP -> rule Workflow -> fenced nodes -> result using isolated SIT schemas."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import text
from test_platform_tasks import ADMIN, service_identity, submit
from test_platform_tasks import inbox as inbox_fixture

from domain.capability import CapabilitySpec, CapabilityVersionDraft
from domain.task_execution import ExecutionFailure
from interfaces.cli.platform import main
from orchestration.capability_service import CapabilityService
from orchestration.platform_worker import PlatformWorker, WorkerPolicy
from orchestration.workflow_executor import WorkflowExecutor

inbox = inbox_fixture
POLICY = WorkerPolicy(lease_seconds=20, heartbeat_seconds=0.2, execution_timeout_seconds=10,
                      cancellation_grace_seconds=0.5)


def literal(value):
    return {"source": "literal", "value": value}


def input_value(pointer):
    return {"source": "input", "pointer": pointer}


def node_value(node, pointer="", **kwargs):
    return {"source": "node", "node": node, "pointer": pointer, **kwargs}


def rule(key, operation="project", **kwargs):
    return {"key": key, "kind": "rule", "rule": {"operation": operation}, **kwargs}


def publish_flow(store, nodes=None, *, definition=None, dependencies=(), output_schema=None):
    service = CapabilityService(store.capabilities)
    service.create("payments", CapabilitySpec(key="flow", kind="workflow", name="New deterministic flow"), "task-admin")
    service.create_draft("payments", "flow", CapabilityVersionDraft(
        version="1", definition=definition or {"nodes": nodes or [rule("project")]},
        dependencies=list(dependencies), output_schema=output_schema or {"type": "object"},
    ), "task-admin")
    service.publish("payments", "flow", "1", "task-admin")


def execute(store, executor=None, policy=POLICY):
    executor = executor or WorkflowExecutor(store)
    return asyncio.run(PlatformWorker(store, "workflow-integration", {"workflow": executor}, policy=policy).run_once())


@pytest.mark.parametrize("account,allowed", [("synthetic-approved", True), ("synthetic-denied", False)])
def test_dependency_mapping_condition_and_result_are_real_not_mocked(inbox, account, allowed):
    client, store = inbox
    definition = {"nodes": [
        rule("allow", depends_on=["check", "normalize"], when=node_value("check", "/value"),
             input_mapping={"approved": literal(True), "account": node_value("normalize", "/account")}),
        rule("check", "equals", depends_on=["normalize"], input_mapping={
            "left": node_value("normalize", "/account"), "right": literal("synthetic-approved")}),
        rule("normalize", input_mapping={"account": input_value("/account")}),
    ], "output_mapping": {
        "approved": node_value("allow", "/approved", required=False, default=False),
        "account": node_value("normalize", "/account"),
    }}
    publish_flow(store, definition=definition, output_schema={"type": "object", "required": ["approved", "account"],
                                                           "properties": {"approved": {"type": "boolean"}}})
    target = submit(client, capability_key="flow", input={"account": account}).headers["Location"]
    assert client.get(target + "/nodes", headers=ADMIN).json() == []
    assert execute(store)
    result = client.get(target + "/result", headers=ADMIN).json()
    assert result["status"] == "succeeded" and result["result"] == {"approved": allowed, "account": account}
    response = client.get(target + "/nodes", headers=ADMIN)
    assert response.headers["Cache-Control"] == "no-store"
    nodes = response.json()
    assert [node["node_key"] for node in nodes] == ["normalize", "check", "allow"]
    assert [node["status"] for node in nodes] == ["succeeded", "succeeded", "succeeded" if allowed else "skipped"]
    assert all(not {"input", "output", "token", "definition"} & node.keys() for node in nodes)
    assert account not in response.text
    assert not execute(store)


@pytest.mark.parametrize("case,error", [
    ("assertion", "WORKFLOW_ASSERTION_FAILED"),
    ("missing", "WORKFLOW_MAPPING_MISSING"),
    ("condition", "WORKFLOW_CONDITION_NOT_BOOLEAN"),
])
def test_failed_node_is_identified_and_downstream_never_starts(inbox, case, error):
    client, store = inbox
    first = rule("first")
    if case == "assertion":
        first = rule("first", "assert", input_mapping={"condition": literal(False)})
    elif case == "missing":
        first["input_mapping"] = {"missing": input_value("/private-missing")}
    else:
        first["when"] = literal(1)
    publish_flow(store, [first, rule("never", depends_on=["first"])])
    target = submit(client, capability_key="flow").headers["Location"]
    assert execute(store)
    result = client.get(target + "/result", headers=ADMIN).json()
    assert result["status"] == "failed" and result["error_code"] == error
    nodes = client.get(target + "/nodes", headers=ADMIN).json()
    assert len(nodes) == 1 and nodes[0]["node_key"] == "first" and nodes[0]["error_code"] == error
    assert nodes[0]["status"] == "failed" and nodes[0]["finish_time"] is not None


@pytest.mark.parametrize("case,error", [("external", "WORKFLOW_ADAPTER_UNAVAILABLE"), ("retry", "WORKFLOW_RETRY_NOT_SUPPORTED")])
def test_whole_graph_preflight_rejects_unsupported_execution_before_any_node(inbox, case, error):
    client, store = inbox
    nodes = [rule("before")]
    dependencies = []
    if case == "external":
        service = CapabilityService(store.capabilities)
        service.create("payments", CapabilitySpec(key="uninstalled-tool", kind="tool", name="Uninstalled"), "task-admin")
        service.create_draft("payments", "uninstalled-tool", CapabilityVersionDraft(
            version="1", definition={"binding_key": "uninstalled", "operation_type": "read_only"},
        ), "task-admin")
        service.publish("payments", "uninstalled-tool", "1", "task-admin")
        nodes.append({"key": "external", "kind": "tool", "capability": {"key": "uninstalled-tool", "version": "1"},
                      "depends_on": ["before"]})
        dependencies = [{"key": "uninstalled-tool", "version": "1"}]
    else:
        nodes[0]["max_attempts"] = 2
    publish_flow(store, nodes, dependencies=dependencies)
    target = submit(client, capability_key="flow").headers["Location"]
    assert execute(store)
    assert client.get(target + "/result", headers=ADMIN).json()["error_code"] == error
    assert client.get(target + "/nodes", headers=ADMIN).json() == []


def test_same_live_attempt_can_reuse_completed_checkpoints_without_duplicate_execution(inbox):
    client, store = inbox
    publish_flow(store, definition={"nodes": [rule("project", input_mapping={"name": input_value("/name")})],
                                    "output_mapping": {"name": node_value("project", "/name")}})
    target = submit(client, capability_key="flow").headers["Location"]
    claim = store.tasks.claim("checkpoint-worker", kinds=("workflow",))
    executor = WorkflowExecutor(store)
    assert asyncio.run(executor(claim)) == {"name": "Synthetic customer"}
    events_before = len(store.logs.list_by_component("workflow_audit"))
    assert asyncio.run(executor(claim)) == {"name": "Synthetic customer"}
    assert len(store.logs.list_by_component("workflow_audit")) == events_before == 2
    assert store.tasks.complete(claim.lease, status="succeeded", result={"name": "Synthetic customer"})
    assert len(client.get(target + "/nodes", headers=ADMIN).json()) == 1


def test_node_timeout_is_independent_of_task_timeout(inbox, monkeypatch):
    client, store = inbox
    publish_flow(store, [rule("slow", timeout_seconds=0.01)])
    target = submit(client, capability_key="flow").headers["Location"]
    executor = WorkflowExecutor(store)

    async def slow(*_args):
        await asyncio.sleep(60)

    monkeypatch.setattr(executor, "_perform", slow)
    assert execute(store, executor)
    result = client.get(target + "/result", headers=ADMIN).json()
    assert result["status"] == "timed_out" and result["error_code"] == "WORKFLOW_NODE_TIMEOUT"
    nodes = client.get(target + "/nodes", headers=ADMIN).json()
    assert nodes[0]["status"] == "failed" and nodes[0]["error_code"] == "WORKFLOW_NODE_TIMEOUT"


def test_task_timeout_finalizes_running_node_atomically(inbox, monkeypatch):
    client, store = inbox
    publish_flow(store, [rule("slow")])
    target = submit(client, capability_key="flow").headers["Location"]
    executor = WorkflowExecutor(store)
    started = []

    async def slow(*_args):
        started.append(True)
        await asyncio.sleep(60)

    monkeypatch.setattr(executor, "_perform", slow)
    assert execute(store, executor, replace(POLICY, execution_timeout_seconds=1))
    assert started
    result = client.get(target + "/result", headers=ADMIN).json()
    assert result["status"] == "timed_out" and result["error_code"] == "EXECUTION_TIMEOUT_RESULT_UNKNOWN"
    nodes = client.get(target + "/nodes", headers=ADMIN).json()
    assert nodes[0]["status"] == "failed" and nodes[0]["error_code"] == "EXECUTION_TIMEOUT_RESULT_UNKNOWN"


def test_expired_node_write_is_fenced_and_recovery_preserves_completed_nodes(inbox, monkeypatch):
    client, store = inbox
    publish_flow(store, [rule("first"), rule("second", depends_on=["first"])])
    target = submit(client, capability_key="flow").headers["Location"]
    real_finish = store.workflows.finish
    finished = []

    def lose_on_second(lease, node_id, **kwargs):
        finished.append(node_id)
        if len(finished) == 2:
            with store.engine.begin() as connection:
                connection.execute(text("UPDATE ai_task_attempt SET lease_expires_at=start_time WHERE id=:id"),
                                   {"id": lease.attempt_id})
        return real_finish(lease, node_id, **kwargs)

    monkeypatch.setattr(store.workflows, "finish", lose_on_second)
    assert execute(store)
    assert client.get(target, headers=ADMIN).json()["status"] == "running"
    assert store.tasks.recover_expired() == 1
    nodes = client.get(target + "/nodes", headers=ADMIN).json()
    assert [node["status"] for node in nodes] == ["succeeded", "failed"]
    assert nodes[1]["error_code"] == "LEASE_EXPIRED_RESULT_UNKNOWN"
    assert not execute(store)


def test_pure_rule_recovery_reuses_verified_checkpoints_with_provenance(inbox, monkeypatch):
    client, store = inbox
    definition = {"nodes": [
        rule("first", input_mapping={"name": input_value("/name")}),
        rule("second", depends_on=["first"], input_mapping={"name": node_value("first", "/name")}),
    ], "output_mapping": {"name": node_value("second", "/name")}}
    publish_flow(store, definition=definition)
    target = submit(client, capability_key="flow").headers["Location"]
    failing = WorkflowExecutor(store)
    perform = failing._perform

    async def fail_second(claim, node, outputs, node_id):
        if node.key == "second":
            raise ExecutionFailure("SYNTHETIC_FAILURE")
        return await perform(claim, node, outputs, node_id)

    monkeypatch.setattr(failing, "_perform", fail_second)
    assert execute(store, failing)
    source_nodes = client.get(target + "/nodes", headers=ADMIN).json()
    assert [node["status"] for node in source_nodes] == ["succeeded", "failed"]
    recovery = client.post(target + "/recover", headers=ADMIN, json={"request_key": "retry"})
    assert recovery.status_code == 202
    recovery_url = "/v2/projects/payments/tasks/" + recovery.json()["id"]
    resumed = WorkflowExecutor(store)
    original_perform = resumed._perform
    performed = []

    async def observe(claim, node, outputs, node_id):
        performed.append(node.key)
        return await original_perform(claim, node, outputs, node_id)

    monkeypatch.setattr(resumed, "_perform", observe)
    assert execute(store, resumed)
    assert performed == ["second"]
    nodes = client.get(recovery_url + "/nodes", headers=ADMIN).json()
    assert nodes[0]["reused_from_node_id"] == source_nodes[0]["id"]
    assert nodes[1]["reused_from_node_id"] is None
    assert client.get(recovery_url + "/result", headers=ADMIN).json()["result"] == {"name": "Synthetic customer"}
    # A later corruption must fail closed, not execute or reuse the altered output.
    with store.engine.begin() as connection:
        connection.execute(text("UPDATE ai_workflow_node SET output=CAST(:output AS jsonb) WHERE id=:id"),
                           {"output": '{"name":"tampered"}', "id": source_nodes[0]["id"]})
    second = client.post(target + "/recover", headers=ADMIN, json={"request_key": "corrupt-checkpoint"})
    second_url = "/v2/projects/payments/tasks/" + second.json()["id"]
    performed.clear()
    assert execute(store, resumed)
    assert performed == []
    assert client.get(second_url + "/result", headers=ADMIN).json()["error_code"] == "WORKFLOW_CHECKPOINT_CONFLICT"


def test_pure_rule_recovery_preserves_skipped_checkpoint(inbox, monkeypatch):
    client, store = inbox
    publish_flow(store, definition={"nodes": [rule("first", when=literal(False)),
        rule("second", depends_on=["first"])], "output_mapping": {}})
    target = submit(client, capability_key="flow").headers["Location"]
    failing = WorkflowExecutor(store)
    perform = failing._perform

    async def fail_second(claim, node, outputs, node_id):
        if node.key == "second":
            raise ExecutionFailure("SYNTHETIC_FAILURE")
        return await perform(claim, node, outputs, node_id)

    monkeypatch.setattr(failing, "_perform", fail_second)
    assert execute(store, failing)
    source = client.get(target + "/nodes", headers=ADMIN).json()
    assert source[0]["status"] == "skipped"
    recovered = client.post(target + "/recover", headers=ADMIN, json={"request_key": "skipped"}).json()
    assert execute(store)
    recovered_url = "/v2/projects/payments/tasks/" + recovered["id"]
    node = client.get(recovered_url + "/nodes", headers=ADMIN).json()[0]
    assert node["status"] == "skipped" and node["reused_from_node_id"] == source[0]["id"]


def test_node_timeline_is_identity_and_capability_scoped(inbox):
    client, store = inbox
    publish_flow(store)
    identity, _, headers = service_identity(store)
    _, _, other_headers = service_identity(store, key="other-workflow-service")
    target = submit(client, headers=headers, capability_key="flow").headers["Location"]
    assert execute(store)
    assert client.get(target + "/nodes", headers=headers).status_code == 200
    assert client.get(target + "/nodes", headers=other_headers).status_code == 404
    assert client.get(target + "/nodes").status_code == 401
    for grant in store.governance.list_grants("payments", identity["id"]):
        if grant["action"] == "task:read":
            store.governance.delete_grant("payments", identity["id"], grant["id"], "task-admin")
    assert client.get(target + "/nodes", headers=headers).status_code == 403


def test_worker_cli_installs_real_rule_executor_and_never_claims_agent(inbox, capsys):
    client, store = inbox
    agent = submit(client, key="agent-left-queued").headers["Location"]
    publish_flow(store)
    target = submit(client, key="workflow-run", capability_key="flow").headers["Location"]
    main(["worker", "--once", "--config-dir", "unused", "--worker-id", "cli-worker"])
    assert client.get(target, headers=ADMIN).json()["status"] == "succeeded"
    assert client.get(agent, headers=ADMIN).json()["status"] == "queued"
    captured = capsys.readouterr()
    assert '"claimed": true' in captured.out and "Synthetic customer" not in captured.out
