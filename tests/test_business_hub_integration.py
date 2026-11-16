"""Real HTTP/owned PostgreSQL/Pi loop; model turns are synthetic and never paid."""

import asyncio
import hashlib
import json

import pytest
from sqlalchemy import text
from test_platform_tasks import ADMIN, service_identity, submit
from test_platform_tasks import inbox as inbox_fixture

from capability_hub.catalog import builtin_catalog
from domain.agent_turn import AgentModelTurn, AgentToolCall, AgentTurnResponse
from domain.capability import CapabilityDependency, CapabilitySpec, CapabilityVersionDraft
from domain.platform_model import ModelUsage
from domain.task import canonical_json
from infra.model_credentials import credential_variable_name
from infra.schema_validation import IsolatedJSONValidator
from interfaces.worker.platform import run_platform_worker
from orchestration.capability_service import CapabilityService
from orchestration.hub_service import HubPublicationService

inbox = inbox_fixture
INPUT = {"query": "Example Trading", "candidates": [
    {"id": "example-1", "name": "Example Trading Ltd"}, {"id": "example-2", "name": "Unrelated Company"},
], "limit": 5}


class CandidateTurns:
    def __init__(self, after=None):
        self.requests = []
        self.after = after

    async def generate_turn(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            values = json.loads(request.conversation.messages[1].content)
            if self.after is not None:
                await self.after()
            turn = AgentModelTurn(kind="tools", tool_calls=(AgentToolCall(
                id="synthetic-provider-call", alias="lookup_candidates", arguments=values,
            ),))
        else:
            assert request.conversation.messages[-1].role == "tool"
            matches = json.loads(request.conversation.messages[-1].content)["matches"]
            turn = AgentModelTurn(kind="final", text=json.dumps({
                "selected_candidate_ids": [match["candidate_id"] for match in matches],
            }))
        return AgentTurnResponse(turn=turn, actual_model="catalog/hub", usage=ModelUsage(total_tokens=7))


def setup(client, store, monkeypatch, *, handler="remitter-search.lookup"):
    monkeypatch.setenv(credential_variable_name("payments", "hub-key"), "synthetic-only-provider-key")
    assert client.post("/v2/projects/payments/model-providers", headers=ADMIN, json={
        "key": "hub-provider", "name": "Synthetic turns only", "base_url": "https://hub.example.invalid/v1",
        "credential_ref": "hub-key", "models": ["catalog/hub"], "max_tokens": 512,
    }).status_code == 201
    service = CapabilityService(store.capabilities, IsolatedJSONValidator())
    service.create("payments", CapabilitySpec(key="hub-turns", kind="model_policy", name="Synthetic turns"), "task-admin")
    service.create_draft("payments", "hub-turns", CapabilityVersionDraft(version="1", definition={
        "interaction": "agent_turn", "provider_key": "hub-provider", "model": "catalog/hub", "max_tokens": 256,
    }), "task-admin")
    service.publish("payments", "hub-turns", "1", "task-admin")
    bundle = builtin_catalog().build("remitter-search", policy=CapabilityDependency(key="hub-turns", version="1"))
    publisher = HubPublicationService(store.capabilities, IsolatedJSONValidator())
    assert all(row["action"] == "published" for row in publisher.publish("payments", bundle, "task-admin")["capabilities"])
    assert all(row["action"] == "reused" for row in publisher.publish("payments", bundle, "task-admin")["capabilities"])
    assert client.post("/v2/projects/payments/tool-bindings", headers=ADMIN, json={
        "key": "remitter-search.lookup", "name": "Approved supplied-candidate lookup", "handler_key": handler,
        "handler_version": "1", "operation_type": "read_only", "max_timeout_seconds": 10,
    }).status_code == 201
    return bundle, service


def execute(store, *, install=True):
    return asyncio.run(run_platform_worker(store, once=True, execution_kinds=("workflow", "agent"),
        workflow_agents=True, hub_bundles=("remitter-search",) if install else ()))


def task(client, headers=ADMIN, *, values=None, version="1"):
    response = submit(client, key="hub-task", headers=headers, capability_key="remitter-search", version=version,
                      input=INPUT if values is None else values)
    assert response.status_code == 202
    return response.headers["Location"]


@pytest.mark.parametrize("empty", [False, True])
def test_custom_steps_agent_business_tool_and_result_share_one_real_attempt(inbox, monkeypatch, empty):
    client, store = inbox
    setup(client, store, monkeypatch)
    _, _, headers = service_identity(store, actions=("task:submit", "task:read", "agent:invoke", "model:invoke",
                                                     "prompt:render", "tool:invoke"))
    turns = CandidateTurns()
    monkeypatch.setattr("interfaces.worker.platform.AgentTurnHTTPTransport", lambda: turns)
    target = task(client, headers, values={} if empty else INPUT)
    assert not execute(store, install=False)
    assert client.get(target + "/attempts", headers=headers).json() == []
    assert execute(store)
    final = client.get(target + "/result", headers=headers).json()
    assert final["status"] == "succeeded"
    assert final["result"]["selected_candidate_ids"] == ([] if empty else ["example-1"])
    assert len(client.get(target + "/attempts", headers=headers).json()) == 1
    nodes = client.get(target + "/nodes", headers=headers).json()
    assert {row["scope"]["activity_key"]: row["kind"] for row in nodes} == {
        None: "result", "normalize-input": "step", "select-candidates": "agent", "validate-selection": "step",
    }
    result = next(row for row in nodes if row["node_key"] == "workflow-result")
    assert all(row["parent_node_id"] == result["id"] for row in nodes if row is not result)
    assert result["output_hash"] == hashlib.sha256(canonical_json(final["result"]).encode()).hexdigest()
    executions = client.get(target + "/agent-executions", headers=headers).json()["items"]
    calls = client.get(target + "/tool-calls", headers=headers).json()
    models = client.get(target + "/model-calls", headers=headers).json()
    if empty:
        assert not turns.requests and not executions and not calls and not models
        assert next(row for row in nodes if row["kind"] == "agent")["status"] == "skipped"
    else:
        assert len(turns.requests) == len(models) == 2 and len(calls) == len(executions) == 1
        agent_node = next(row for row in nodes if row["kind"] == "agent")
        assert executions[0]["parent_node_id"] == agent_node["id"]
        assert executions[0]["output_hash"] == agent_node["output_hash"]
        assert calls[0]["owner"]["kind"] == "agent_step" and calls[0]["handler_key"] == "remitter-search.lookup"
    plan = client.get("/v2/projects/payments/capabilities/remitter-search/versions/1/plan", headers=ADMIN)
    assert plan.status_code == 200 and plan.json()["control_flow"] == "code"
    assert plan.json()["implementation"]["digest"] and "depends_on" not in plan.json()["activities"][0]
    events = client.get(target + "/events", headers=headers).json()["items"]
    assert events[-1]["event_type"] == "task_succeeded"
    assert "Example Trading" not in json.dumps([nodes, executions, calls, models, events])


@pytest.mark.parametrize("mismatch", ["digest", "implementation-role"])
def test_wrong_code_reference_remains_queued_without_consuming_an_attempt(inbox, monkeypatch, mismatch):
    client, store = inbox
    bundle, service = setup(client, store, monkeypatch)
    artifact = next(item.draft for item in bundle.capabilities if item.kind == "workflow")
    artifact.version = "wrong-" + mismatch
    if mismatch == "digest":
        artifact.definition["implementation"]["digest"] = "0" * 64
    else:
        artifact.definition["implementation"] = next(reference.model_dump() for reference in bundle.references
                                                      if reference.key == "remitter-search.lookup")
    service.create_draft("payments", "remitter-search", artifact, "task-admin")
    service.publish("payments", "remitter-search", artifact.version, "task-admin")
    turns = CandidateTurns()
    monkeypatch.setattr("interfaces.worker.platform.AgentTurnHTTPTransport", lambda: turns)
    target = task(client, version=artifact.version)
    assert not execute(store)
    assert client.get(target, headers=ADMIN).json()["status"] == "queued"
    assert client.get(target + "/attempts", headers=ADMIN).json() == [] and not turns.requests


@pytest.mark.parametrize("missing,expected", [("agent:invoke", "AGENT_ACCESS_DENIED"), ("tool:invoke", "TOOL_ACCESS_DENIED"),
                                             ("binding", "TOOL_BINDING_DISABLED"), ("implementation", "TOOL_IMPLEMENTATION_MISMATCH")])
def test_installation_does_not_grant_business_resource_access(inbox, monkeypatch, missing, expected):
    client, store = inbox
    setup(client, store, monkeypatch, handler="json.project" if missing == "implementation" else "remitter-search.lookup")
    actions = {"task:submit", "task:read", "agent:invoke", "model:invoke", "prompt:render", "tool:invoke"} - {missing}
    _, _, headers = service_identity(store, actions=tuple(actions))
    if missing == "binding":
        assert client.patch("/v2/projects/payments/tool-bindings/remitter-search.lookup", headers=ADMIN,
                            json={"enabled": False}).status_code == 200
    turns = CandidateTurns()
    monkeypatch.setattr("interfaces.worker.platform.AgentTurnHTTPTransport", lambda: turns)
    target = task(client, headers)
    assert execute(store)
    result = client.get(target + "/result", headers=headers).json()
    assert result["status"] == "failed" and result["error_code"] == expected and result["result"] is None
    with store.engine.connect() as connection:
        assert connection.scalar(text("SELECT result IS NULL FROM ai_task WHERE id=:id"), {"id": target.rsplit("/", 1)[1]}) is True
    assert not turns.requests and client.get(target + "/tool-calls", headers=headers).json() == []


def test_tool_grant_revoked_during_turn_retains_usage_without_dispatch(inbox, monkeypatch):
    client, store = inbox
    setup(client, store, monkeypatch)
    identity, _, headers = service_identity(store, actions=("task:submit", "task:read", "agent:invoke", "model:invoke",
                                                            "prompt:render", "tool:invoke"))
    grant = next(row for row in store.governance.list_grants("payments", identity["id"]) if row["action"] == "tool:invoke")

    async def revoke():
        await asyncio.to_thread(store.governance.delete_grant, "payments", identity["id"], grant["id"], "task-admin")

    turns = CandidateTurns(after=revoke)
    monkeypatch.setattr("interfaces.worker.platform.AgentTurnHTTPTransport", lambda: turns)
    target = task(client, headers)
    assert execute(store)
    final = client.get(target + "/result", headers=headers).json()
    assert final["status"] == "failed" and final["error_code"] == "MODEL_AGENT_ACCESS_DENIED"
    assert len(turns.requests) == 1 and client.get(target + "/tool-calls", headers=headers).json() == []
    calls = client.get(target + "/model-calls", headers=headers).json()
    assert calls[0]["status"] == "failed" and calls[0]["total_tokens"] == 7
