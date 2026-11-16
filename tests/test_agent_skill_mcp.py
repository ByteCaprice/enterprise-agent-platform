"""Offline joint integration: real Pi loop + governed Skill + approved MCP JSON wire.

Catalog/ledger ports and model responses are synthetic; no Task/SQL/SIT or live model
coverage is implied. Skills remain versioned guidance, not executable plugins.
"""

import asyncio
import hashlib
import json
import socket
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_platform_mcp import INPUT, OUTPUT, VALUES, MockMCP, catalog

from domain.agent_runtime import AgentRuntimeRegistry
from domain.agent_turn import AgentModelTurn, AgentToolCall, AgentTurnResponse
from domain.capability import CapabilityVersionDraft, CapabilityVersionRecord
from domain.execution_ports import ExecutionResources
from domain.platform_agent import AgentDefinition
from domain.platform_model import ModelProviderRecord, ModelSecret, ModelUsage
from domain.platform_tool import ToolBindingRecord
from domain.task import TaskRecord, canonical_json
from domain.task_execution import ExecutionFailure, TaskClaim, TaskLease
from framework.agent_runtime.pi.adapter import PiRuntime
from infra.schema_validation import IsolatedJSONValidator
from infra.skill_files import load_skill_bundle
from orchestration.agent_executor import AgentExecutor
from orchestration.agent_model_gateway import PlatformAgentModelGateway
from orchestration.execution_authorization import ExecutionAuthorizer
from orchestration.mcp_tools import mcp_readonly_tools
from orchestration.model_policy_resolver import ModelPolicyResolver
from orchestration.platform_tool_gateway import PlatformToolGateway
from orchestration.prompt_renderer import PromptRenderer

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PROMPT = "Use only the approved lookup tool and return JSON."
SKILL = "Review approved evidence before answering; return only the selected name."
RESULT = {"name": "Synthetic"}
EMPTY = {"type": "object", "maxProperties": 0}


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class MemoryCatalog:
    def __init__(self):
        self.identities, self.artifacts = {}, {}

    def publish(self, key, kind, definition, *, input_schema=EMPTY, output_schema=OUTPUT, dependencies=()):
        draft = CapabilityVersionDraft(version="1", definition=definition, input_schema=input_schema,
                                       output_schema=output_schema, dependencies=list(dependencies))
        self.identities[key] = {"id": key, "project_key": "payments", "key": key, "kind": kind}
        artifact = CapabilityVersionRecord(**draft.model_dump(), id=key + "-v1", capability_id=key,
                                          status="published", content_hash=draft.fingerprint(),
                                          created_by="synthetic", create_time=NOW,
                                          published_by="synthetic", publish_time=NOW)
        artifact.require_published(version="1")
        self.artifacts[key] = artifact
        return artifact

    def get(self, project_key, key):
        assert project_key == "payments"
        return dict(self.identities[key])

    def get_version(self, project_key, key, version):
        assert (project_key, version) == ("payments", "1")
        return self.artifacts[key].model_copy(deep=True)


class Grants:
    """Only the governance reader is synthetic; ExecutionAuthorizer is production."""

    def __init__(self):
        self.grants = {("task:submit", "review-agent"), ("model:invoke", "review-turns"),
                       ("prompt:render", "review-instructions"), ("tool:invoke", "readonly-lookup"),
                       ("skill:use", "review-guide")}
        self.checked = []

    def get_project(self, project_key):
        assert project_key == "payments"
        return {"enabled": True}

    def execution_resources(self, project_key, identity_id, action):
        assert (project_key, identity_id) == ("payments", "synthetic")
        self.checked.append(action)
        return {key for grant, key in self.grants if grant == action}


class Ledger:
    """Controlled port records, not a second Agent engine or storage implementation."""

    def __init__(self, source, binding, claim):
        self.source, self.tool_binding, self.claim = source, binding, claim
        self.events, self.inputs, self.accepted = [], [], []
        self.after_tool = lambda: None
        tool = source.artifacts["readonly-lookup"]
        self.tool_step = SimpleNamespace(id="tool-step", dispatch_id="approved-handle",
                                         resource_key="readonly-lookup", resource_version="1",
                                         resource_hash=tool.content_hash)

    def binding(self, project_key, key):
        assert (project_key, key) == ("payments", "directory-binding")
        return self.tool_binding

    def begin(self, lease, *, runtime_profile, parent_node_id, input_values):
        assert lease == self.claim.lease and parent_node_id is None and input_values == {}
        assert runtime_profile == PiRuntime.descriptor.profile
        self.events.append("begin")
        return SimpleNamespace(id="execution", agent_key="review-agent", agent_version="1",
                               agent_hash=self.claim.artifact.content_hash)

    def start_turn(self, lease, execution_id, *, input_hash):
        assert lease == self.claim.lease and execution_id == "execution"
        self.events.append("start_turn")
        self.inputs.append(input_hash)
        return SimpleNamespace(id=f"turn-{len(self.inputs)}"), "model-call"

    def accept_turn(self, lease, execution_id, step_id, **values):
        assert (lease, execution_id, step_id) == (self.claim.lease, "execution", f"turn-{len(self.inputs)}")
        assert values["actual_model"] == "catalog/primary"
        assert values["evidence"].acceptance == "accepted"
        self.events.append("accept_turn")
        self.accepted.append(values)
        if values["intents"]:
            intent, = values["intents"]
            assert (intent.alias, intent.source_call_id, intent.arguments) == ("lookup", "provider-source", VALUES)
            assert values["final_result"] is None
            return [self.tool_step]
        assert values["final_result"] == RESULT
        return []

    def start_tool(self, lease, execution_id, dispatch_id, *, arguments):
        assert (lease, execution_id, dispatch_id) == (self.claim.lease, "execution", "approved-handle")
        assert arguments == VALUES
        self.events.append("start_tool")
        self.tool_input_hash = digest(arguments)
        return self.tool_step, "tool-call"

    def finish_tool(self, lease, execution_id, step_id, *, output):
        assert (lease, execution_id, step_id, output) == (self.claim.lease, "execution", "tool-step", RESULT)
        self.events.append("finish_tool")
        self.tool_output_hash = digest(output)
        self.after_tool()

    def finish(self, lease, execution_id, *, result):
        assert (lease, execution_id, result) == (self.claim.lease, "execution", RESULT)
        self.events.append("finish")

    def reject_call(self, lease, execution_id, step_id, **values):
        self.events.append(("reject", values["error_code"]))

    def fail(self, lease, execution_id, *, error_code):
        self.events.append(("fail", error_code))


class MockModel:
    """Explicit replacement for external model transport, never the Agent loop."""

    def __init__(self, source):
        self.source, self.requests, self.replies = source, [], []

    def resolve(self, project_key, reference):
        assert (project_key, reference) == ("payments", "model-readonly")
        return ModelSecret(reference, "synthetic-model-secret")

    async def generate_turn(self, request):
        self.requests.append(request)
        system = request.conversation.messages[0]
        skill = self.source.artifacts["review-guide"]
        guidance = [{"key": "review-guide", "version": "1", "content_hash": skill.content_hash, "instructions": SKILL}]
        assert system.role == "system"
        assert system.content == PROMPT + "\n\nVersioned Skill guidance (not tool authorization):\n" + canonical_json(guidance)
        descriptor, = request.tools
        assert descriptor.alias == "lookup" and descriptor.input_schema == INPUT
        exposed = system.content + descriptor.model_dump_json()
        for forbidden in ("synthetic-secret", "synthetic-model-secret", "mcp.example.com", "credential_ref"):
            assert forbidden not in exposed
        assert request.model == "catalog/primary" and request.secret.value == "synthetic-model-secret"
        if len(self.requests) == 1:
            turn = AgentModelTurn(kind="tools", tool_calls=(AgentToolCall(
                id="provider-source", alias="lookup", arguments=VALUES),))
        else:
            assert len(self.requests) == 2
            message = request.conversation.messages[-1]
            assert message.role == "tool" and message.tool_call_id == "provider-source"
            assert json.loads(message.content) == RESULT
            turn = AgentModelTurn(kind="final", text=canonical_json(RESULT))
        reply = AgentTurnResponse(turn=turn, actual_model="catalog/primary", usage=ModelUsage(total_tokens=3))
        self.replies.append(reply)
        return reply


class ObservedPi(PiRuntime):
    async def run(self, start, callbacks):
        self.start = start
        self.result = await super().run(start, callbacks)
        return self.result


def build(*, skill_definition=None):
    source = MemoryCatalog()
    source.publish("review-instructions", "prompt", {"template": PROMPT}, output_schema={"type": "string"})
    source.publish("review-guide", "skill", skill_definition or {"instructions": SKILL})
    source.publish("readonly-lookup", "tool", {"binding_key": "directory-binding", "operation_type": "read_only",
                                              "timeout_seconds": 10.0}, input_schema=INPUT)
    source.publish("review-turns", "model_policy", {"interaction": "agent_turn", "provider_key": "synthetic",
                   "model": "catalog/primary", "timeout_seconds": 15.0, "max_tokens": 128},
                   input_schema={"type": "object", "required": ["messages", "tools"], "properties": {
                       "messages": {"type": "array", "minItems": 2}, "tools": {"type": "array", "minItems": 1}},
                       "additionalProperties": False}, output_schema=AgentModelTurn.model_json_schema())
    definition = AgentDefinition.model_validate({"definition_version": "agent.v1", "runtime": "pi",
        "instruction_prompt": {"key": "review-instructions", "version": "1"},
        "model_policy": {"key": "review-turns", "version": "1"},
        "tools": [{"alias": "lookup", "capability": {"key": "readonly-lookup", "version": "1"}}],
        "skills": [{"key": "review-guide", "version": "1"}],
        "limits": {"max_turns": 2, "max_tool_calls": 1, "timeout_seconds": 30.0}})
    artifact = source.publish("review-agent", "agent", definition.model_dump(mode="json"),
                              dependencies=[ref.model_dump() for ref in definition.dependency_refs()])
    task = TaskRecord(id="task", project_key="payments", capability_key="review-agent", capability_kind="agent",
                      version="1", content_hash=artifact.content_hash, status="running",
                      submitted_by="service:synthetic", create_time=NOW, finish_time=None)
    claim = TaskClaim(task, TaskLease("task", "attempt", "worker", "synthetic-lease"), {}, artifact)
    approved, mcp, grants, runtime = catalog(), MockMCP(), Grants(), ObservedPi()
    pin = approved.tools[0]
    binding = ToolBindingRecord(key="directory-binding", name="Synthetic", project_key="payments",
        handler_key=pin.handler_key, handler_version=pin.handler_version(approved.connections[0]),
        operation_type="read_only", max_timeout_seconds=10.0, enabled=True, created_by="synthetic", create_time=NOW)
    ledger, model = Ledger(source, binding, claim), MockModel(source)
    resources = ExecutionResources(source, ExecutionAuthorizer(grants, {}), IsolatedJSONValidator())
    tools = PlatformToolGateway(resources, ledger, ledger, mcp_readonly_tools(approved, transport=mcp.transport()))
    provider = ModelProviderRecord(key="synthetic", name="Synthetic", project_key="payments",
        base_url="https://model.example.com/v1", credential_ref="model-readonly", models=["catalog/primary"],
        max_tokens=128, enabled=True, created_by="synthetic", create_time=NOW)

    def read_provider(project_key, key):
        assert (project_key, key) == ("payments", "synthetic")
        return provider

    policies = ModelPolicyResolver(resources, SimpleNamespace(provider=read_provider))
    gateway = PlatformAgentModelGateway(resources, policies, ledger, model, model, tools)
    executor = AgentExecutor(gateway, AgentRuntimeRegistry([runtime]), lifecycle=ledger, prompts=PromptRenderer(source))
    return SimpleNamespace(claim=claim, executor=executor, model=model, mcp=mcp, grants=grants,
                           runtime=runtime, ledger=ledger, source=source)


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Offline joint test attempted external DNS/socket access")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


def execute(case):
    async def run():
        await case.runtime.check_ready()  # Real installed Node/Pi 1.4.0 handshake, no install/fallback.
        return await case.executor(case.claim)

    return asyncio.run(run())


def assert_one_mcp_call(case):
    assert [payload.get("method", request.method) for request, payload in case.mcp.requests] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call", "DELETE"]
    call = case.mcp.requests[3][1]
    assert call["params"] == {"name": "project", "arguments": VALUES}
    assert len(case.mcp.options) == 1


def test_real_pi_governed_skill_model_mcp_model_final(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: review-guide\ndescription: Review evidence\n---\n" + SKILL)
    imported = load_skill_bundle(path)
    case = build(skill_definition=imported.capabilities[0].draft.definition)
    assert execute(case) == RESULT
    assert_one_mcp_call(case)
    assert case.ledger.events == ["begin", "start_turn", "accept_turn", "start_tool", "finish_tool",
                                  "start_turn", "accept_turn", "finish"]
    assert case.runtime.result.turns == 2 and case.runtime.result.tool_calls == 1
    start = case.runtime.start
    assert (start.max_turns, start.max_tool_calls) == (2, 1) and 0 < start.timeout_seconds <= 30
    assert start.instructions == case.model.requests[0].conversation.messages[0].content
    assert start.tools == case.model.requests[0].tools
    for forbidden in ("synthetic-secret", "synthetic-model-secret", "synthetic-lease", "mcp.example.com", "model.example.com"):
        assert forbidden not in start.model_dump_json()
    for index, request in enumerate(case.model.requests):
        payload = {**request.conversation.model_dump(mode="json"),
                   "tools": [tool.model_dump(mode="json") for tool in request.tools]}
        assert case.ledger.inputs[index] == digest({"input": payload,
            "policy_hash": case.source.artifacts["review-turns"].content_hash, "agent_hash": case.claim.artifact.content_hash})
        assert case.ledger.accepted[index]["output_hash"] == digest(case.model.replies[index].turn.model_dump(mode="json"))
    assert case.ledger.tool_input_hash == digest(VALUES) and case.ledger.tool_output_hash == digest(RESULT)
    assert {"skill:use", "tool:invoke"} <= set(case.grants.checked)


@pytest.mark.parametrize("action,key,code", [
    ("skill:use", "review-guide", "AGENT_SKILL_ACCESS_DENIED"),
    ("tool:invoke", "readonly-lookup", "TOOL_ACCESS_DENIED"),
])
def test_missing_explicit_grant_blocks_first_model_and_mcp(action, key, code):
    case = build()
    case.grants.grants.remove((action, key))
    with pytest.raises(ExecutionFailure, match=f"^{code}$"):
        execute(case)
    assert not case.model.requests and not case.mcp.requests and not case.mcp.options
    assert not case.ledger.events


def test_skill_revoked_after_mcp_success_blocks_next_model_resolution():
    case = build()
    case.ledger.after_tool = lambda: case.grants.grants.remove(("skill:use", "review-guide"))
    with pytest.raises(ExecutionFailure, match="^AGENT_SKILL_ACCESS_DENIED$"):
        execute(case)
    assert_one_mcp_call(case)
    assert len(case.model.requests) == len(case.ledger.inputs) == len(case.ledger.accepted) == 1
    assert "finish_tool" in case.ledger.events and "finish" not in case.ledger.events
    assert case.ledger.tool_output_hash == digest(RESULT)
