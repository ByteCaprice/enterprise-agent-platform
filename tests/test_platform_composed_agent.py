"""Two composed smokes: actual Pi/local turns, one lease, delegation and durable history."""

import asyncio
import json

import pytest
from test_platform_agent_broker import MockTurns
from test_platform_agent_storage import root_fixture
from test_platform_tasks import ADMIN, service_identity, submit
from test_platform_tasks import inbox as inbox_fixture

from domain.capability import CapabilitySpec, CapabilityVersionDraft
from framework.readonly_tools import builtin_readonly_tools
from infra.model_credentials import EnvironmentCredentialResolver
from interfaces.worker.platform import run_platform_worker
from orchestration.agent_model_gateway import PlatformAgentModelGateway
from orchestration.capability_service import CapabilityService
from orchestration.platform_tool_gateway import PlatformToolGateway

inbox = inbox_fixture


@pytest.mark.parametrize("delegated", [True, False])
def test_workflow_agent_keeps_one_root_claim_and_atomic_safe_history(inbox, monkeypatch, delegated):
    client, store = inbox
    _, _, unused_root = root_fixture(client, store, monkeypatch, begin=False, claim_task=False)
    service = CapabilityService(store.capabilities)
    service.create("payments", CapabilitySpec(key="agent-flow", name="Composed Agent", kind="workflow"), "task-admin")
    service.create_draft("payments", "agent-flow", CapabilityVersionDraft(
        version="1", definition={
            "nodes": [{"key": "review", "kind": "agent", "capability": {"key": "root-agent", "version": "1"},
                       "input_mapping": {"name": {"source": "input", "pointer": "/name"}}}],
            "output_mapping": {"name": {"source": "node", "node": "review", "pointer": "/name"}},
        }, dependencies=[{"key": "root-agent", "version": "1"}],
    ), "task-admin")
    service.publish("payments", "agent-flow", "1", "task-admin")
    actions = ["task:submit", "task:read", "task:cancel", "model:invoke", "prompt:render", "tool:invoke"]
    if delegated:
        actions.append("agent:invoke")
    _, _, headers = service_identity(store, actions=tuple(actions))
    target = submit(client, key="composed", headers=headers, capability_key="agent-flow",
                    input={"name": "Synthetic customer", "private": "not-delegated"}).headers["Location"]
    assert not asyncio.run(run_platform_worker(store, once=True))
    assert client.get(target, headers=headers).json()["status"] == "queued"
    turns = MockTurns()
    tools = PlatformToolGateway(store, builtin_readonly_tools())
    gateway = PlatformAgentModelGateway(store, turns, EnvironmentCredentialResolver(), tools)
    assert asyncio.run(run_platform_worker(store, once=True, workflow_agents=True,
                                           execution_kinds=("workflow",), agent_gateway=gateway))
    result = client.get(target + "/result", headers=headers).json()
    attempts = client.get(target + "/attempts", headers=headers).json()
    assert len(attempts) == 1
    if delegated:
        assert result["status"] == "succeeded" and result["result"] == {"name": "Synthetic name"}
        assert json.loads(turns.requests[0].conversation.messages[1].content) == {"name": "Synthetic customer"}
        nodes = client.get(target + "/nodes", headers=headers).json()
        executions = client.get(target + "/agent-executions", headers=headers).json()["items"]
        assert executions[0]["parent_node_id"] == nodes[0]["id"]
        assert nodes[0]["output_hash"] == executions[0]["output_hash"]
        assert len(turns.requests) == 2 and client.get(unused_root, headers=ADMIN).json()["status"] == "queued"
    else:
        assert result["status"] == "failed" and result["error_code"] == "AGENT_ACCESS_DENIED"
        assert not turns.requests and client.get(target + "/agent-executions", headers=headers).json()["items"] == []
    events, after = [], 0
    while True:
        page = client.get(target + f"/events?after_sequence={after}&limit=2", headers=headers).json()
        events.extend(page["items"])
        after = page["next_after"]
        if not page["has_more"]:
            break
    assert page["history_complete"] and [event["sequence"] for event in events] == list(range(1, page["head_sequence"] + 1))
    assert events[0]["event_type"] == "task_submitted" and events[-1]["event_type"] == ("task_succeeded" if delegated else "task_failed")
    assert "not-delegated" not in json.dumps(events) and "provider-source" not in json.dumps(events)
    assert client.get("/console/").status_code == 404
    assert client.get("/v2/session", headers=headers).json()["identity_id"] is not None
