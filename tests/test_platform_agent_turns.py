"""Small normalized-turn and actual HTTP-shape smokes, entirely in memory."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError

from domain.agent_turn import AgentConversation, AgentToolDescriptor, AgentTurnRequest
from domain.capability import AgentTurnPolicyDefinition, CapabilityKind, CapabilityVersionDraft, ModelPolicyDefinition
from domain.platform_model import ModelSecret, ModelTransportFailure
from infra.agent_turn_http import AgentTurnHTTPTransport
from orchestration.capability_service import CapabilityService


def response(*, invalid=False):
    return {"model": "catalog/primary", "choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": "provider-call", "type": "function", "function": {
            "name": "lookup", "arguments": '{"x":1,"x":2}' if invalid else '{"data":{"name":"Synthetic"},"fields":["name"]}',
        }}],
    }}], "usage": {"total_tokens": 9}}


def test_agent_mode_is_explicit_and_does_not_weaken_completion_policy():
    policy = {"provider_key": "mock", "model": "catalog/primary"}
    assert ModelPolicyDefinition.model_validate(policy).interaction == "completion"
    with pytest.raises(ValidationError):
        AgentTurnPolicyDefinition.model_validate(policy)
    with pytest.raises(ValidationError):
        ModelPolicyDefinition.model_validate({**policy, "interaction": "agent_turn"})
    with pytest.raises(ValidationError):
        AgentTurnPolicyDefinition.model_validate({**policy, "interaction": "agent_turn", "fallback_models": ["other"]})
    with pytest.raises(ValidationError):
        AgentTurnPolicyDefinition.model_validate({**policy, "interaction": "agent_turn", "request_profile": "arbitrary"})
    with pytest.raises(ValidationError):
        CapabilityService._validate_definition(CapabilityKind.AGENT, CapabilityVersionDraft(
            version="1", definition={"definition_version": "agent.future", "name": "prototype", "route_tags": []},
        ))
    with pytest.raises(ValidationError):
        CapabilityService._validate_definition(CapabilityKind.AGENT, CapabilityVersionDraft(
            version="1", definition={"name": "old-agent", "version": "1", "route_tags": []},
        ))


@pytest.mark.parametrize("profile", ["openai_chat", "openrouter_non_reasoning"])
def test_http_turn_uses_fixed_egress_tools_and_preserves_usage_for_rejected_response(profile):
    seen = []
    reasoning = response()
    reasoning["choices"][0]["message"]["reasoning_content"] = "Synthetic private reasoning"
    replies = [response(), response(invalid=True), reasoning]

    async def http(request):
        assert request.url.host == "8.8.8.8" and request.headers["host"] == "api.example.com"
        assert request.extensions["sni_hostname"] == "api.example.com"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=replies.pop(0), headers={"x-request-id": "safe-id"})

    async def resolve(_host):
        return ["8.8.8.8"]

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False and kwargs["verify"] is True
        return httpx.AsyncClient(transport=httpx.MockTransport(http), **kwargs)

    request = AgentTurnRequest("https://api.example.com/v1", "catalog/primary", AgentConversation.model_validate({
        "messages": [{"role": "system", "content": "Synthetic instructions"}, {"role": "user", "content": "{}"}],
    }), (AgentToolDescriptor(alias="lookup", description="Approved", input_schema={"type": "object"}),),
        ModelSecret("ref", "synthetic-only"), 2, 32, request_profile=profile)

    async def run():
        transport = AgentTurnHTTPTransport(client_factory=client, resolver=resolve)
        valid = await transport.generate_turn(request)
        invalid = await transport.generate_turn(request)
        rejected_reasoning = await transport.generate_turn(request)
        assert valid.turn is not None and valid.turn.kind == "tools" and valid.turn.tool_calls[0].id == "provider-call"
        assert invalid.turn is None and invalid.error_code == "MODEL_RESPONSE_INVALID"
        assert valid.usage.total_tokens == invalid.usage.total_tokens == 9
        assert invalid.request_id == "safe-id"
        assert rejected_reasoning.turn is None and rejected_reasoning.error_code == "MODEL_REASONING_UNSUPPORTED"
        assert rejected_reasoning.usage.total_tokens == 9
        assert "Synthetic private reasoning" not in rejected_reasoning.model_dump_json()
        # A trusted caller bypassing the typed policy must not create a new
        # implicit request profile or cause an extra outbound request.
        with pytest.raises(ModelTransportFailure, match="^MODEL_REQUEST_PROFILE_UNSUPPORTED$"):
            await transport.generate_turn(replace(request, request_profile="unknown"))

    asyncio.run(run())
    assert len(seen) == 3
    assert seen[0]["tool_choice"] == "auto" and seen[0]["parallel_tool_calls"] is False and seen[0]["stream"] is False
    assert seen[0]["tools"][0]["function"]["name"] == "lookup" and "response_format" not in seen[0]
    if profile == "openrouter_non_reasoning":
        assert seen[0]["reasoning"] == {"enabled": False, "exclude": True}
    else:
        assert "reasoning" not in seen[0]


def test_request_conversation_rejects_partial_or_reordered_tool_results():
    messages = [{"role": "system", "content": "instructions"}, {"role": "user", "content": "{}"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "source", "alias": "lookup", "arguments": {}}]}]
    with pytest.raises(ValidationError):
        AgentConversation.model_validate({"messages": messages})
    with pytest.raises(ValidationError):
        AgentConversation.model_validate({"messages": [*messages, {"role": "tool", "tool_call_id": "wrong", "content": "{}"}]})
