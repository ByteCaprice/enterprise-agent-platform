"""Three controlled broker smokes, not a Pi adapter or hand-written Agent runtime."""

import asyncio

import pytest
from sqlalchemy import text
from test_platform_agent_storage import root_fixture
from test_platform_tasks import inbox as inbox_fixture
from test_platform_tasks import service_identity

from domain.agent_invocation import AgentInvocation
from domain.agent_turn import AgentModelTurn, AgentToolCall, AgentTurnResponse
from domain.platform_model import ModelUsage
from domain.task_execution import ExecutionFailure
from framework.agent_runtime.pi.adapter import PiRuntime
from framework.readonly_tools import builtin_readonly_tools
from infra.model_credentials import EnvironmentCredentialResolver
from orchestration.agent_broker import AgentBroker
from orchestration.agent_model_gateway import PlatformAgentModelGateway
from orchestration.platform_tool_gateway import PlatformToolGateway

inbox = inbox_fixture


class MockTurns:
    def __init__(self, *, denied=False):
        self.requests = []
        self.denied = denied

    async def generate_turn(self, request):
        self.requests.append(request)
        if self.denied:
            return AgentTurnResponse(error_code="MODEL_RESPONSE_REFUSED", actual_model="catalog/primary", usage=ModelUsage(total_tokens=11))
        if len(self.requests) == 1:
            return AgentTurnResponse(turn=AgentModelTurn(kind="tools", tool_calls=(AgentToolCall(
                id="provider-source", alias="lookup", arguments={"data": {"name": "Synthetic name"}, "fields": ["name"]},
            ),)), actual_model="catalog/primary", usage=ModelUsage(total_tokens=7))
        assert request.conversation.messages[-1].role == "tool"
        assert request.conversation.messages[-1].tool_call_id == "provider-source"
        return AgentTurnResponse(turn=AgentModelTurn(kind="final", text='{"name":"Synthetic name"}'),
                                 actual_model="catalog/primary", usage=ModelUsage(total_tokens=3))


def make_broker(claim, store, transport):
    tools = PlatformToolGateway(store, builtin_readonly_tools())
    return AgentBroker(AgentInvocation.from_root(claim), PlatformAgentModelGateway(store, transport, EnvironmentCredentialResolver(), tools), PiRuntime.descriptor)


def test_skill_is_pinned_in_context_and_revocation_blocks_next_model_call(inbox, monkeypatch):
    client, store = inbox
    identity, _, headers = service_identity(store, actions=("task:submit", "task:read", "model:invoke",
                                                             "prompt:render", "tool:invoke", "skill:use"))
    claim, _, _ = root_fixture(client, store, monkeypatch, begin=False, headers=headers,
                               skill="Review the evidence before answering.")
    transport = MockTurns()
    broker = make_broker(claim, store, transport)

    async def run():
        start = await broker.open()
        assert '"key":"review-guide"' in start.instructions
        assert "Review the evidence before answering." in start.instructions
        turn = await broker.request_model()
        await broker.dispatch_tool(turn.tool_calls[0].dispatch_id)
        for grant in store.governance.list_grants("payments", identity["id"]):
            if grant["action"] == "skill:use":
                store.governance.delete_grant("payments", identity["id"], grant["id"], "task-admin")
        with pytest.raises(ExecutionFailure, match="AGENT_SKILL_ACCESS_DENIED"):
            await broker.request_model()
        assert len(transport.requests) == 1

    asyncio.run(run())


def test_broker_drives_real_governed_tool_and_final_using_same_ledger_not_fake_nodes(inbox, monkeypatch):
    client, store = inbox
    _, _, headers = service_identity(store, actions=("task:submit", "task:read", "model:invoke", "prompt:render", "tool:invoke"))
    claim, _, target = root_fixture(client, store, monkeypatch, begin=False, headers=headers)
    transport = MockTurns()
    broker = make_broker(claim, store, transport)

    async def run():
        await broker.open()
        turn = await broker.request_model()
        assert turn.kind == "tools" and "provider-source" not in turn.model_dump_json()
        handle = turn.tool_calls[0].dispatch_id
        # Runtime-side mutation is not the broker's authoritative arguments.
        turn.tool_calls[0].arguments["data"]["name"] = "tampered"
        assert await broker.dispatch_tool(handle) == {"name": "Synthetic name"}
        assert (await broker.request_model()).kind == "final"
        result = await broker.finish()
        assert store.tasks.complete(claim.lease, status="succeeded", result=result)

    asyncio.run(run())
    assert len(transport.requests) == 2
    assert client.get(target + "/result", headers=headers).json()["result"] == {"name": "Synthetic name"}
    assert client.get(target + "/tool-calls", headers=headers).json()[0]["owner"]["kind"] == "agent_step"
    with store.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM ai_workflow_node")) == 0
        assert connection.scalar(text("SELECT sum(total_tokens) FROM ai_platform_model_call")) == 10


def test_refusal_retains_usage_and_poisons_broker_without_accepting_intents(inbox, monkeypatch):
    client, store = inbox
    claim, _, target = root_fixture(client, store, monkeypatch, begin=False)
    transport = MockTurns(denied=True)
    broker = make_broker(claim, store, transport)

    async def run():
        await broker.open()
        with pytest.raises(ExecutionFailure, match="MODEL_RESPONSE_REFUSED"):
            await broker.request_model()
        with pytest.raises(ExecutionFailure, match="AGENT_PROTOCOL_STATE_INVALID"):
            await broker.request_model()

    asyncio.run(run())
    assert len(transport.requests) == 1
    store.tasks.complete(claim.lease, status="failed", error_code="MODEL_RESPONSE_REFUSED")
    call = client.get(target + "/model-calls", headers={"x-api-key": "synthetic-task-admin"}).json()[0]
    assert call["status"] == "failed" and call["total_tokens"] == 11 and call["output_hash"] is None
    with store.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM ai_agent_step WHERE kind='tool_call'")) == 0


def test_missing_tool_authority_prevents_first_model_request(inbox, monkeypatch):
    client, store = inbox
    _, _, headers = service_identity(store, actions=("task:submit", "task:read", "model:invoke", "prompt:render"))
    claim, _, _ = root_fixture(client, store, monkeypatch, begin=False, headers=headers)
    transport = MockTurns()

    async def run():
        with pytest.raises(ExecutionFailure, match="TOOL_ACCESS_DENIED"):
            await make_broker(claim, store, transport).open()

    asyncio.run(run())
    assert not transport.requests
    store.tasks.complete(claim.lease, status="failed", error_code="TOOL_ACCESS_DENIED")
