"""Three cheap contract smokes; no claim of Agent/Pi runtime integration coverage."""

import pytest
from pydantic import TypeAdapter, ValidationError

from domain.agent_runtime import AgentRuntimeDescriptor, AgentRuntimeRegistry
from domain.capability import SkillDefinition
from domain.platform_agent import AgentDefinition, CallOwner


def definition():
    return {
        "definition_version": "agent.v1", "runtime": "pi",
        "instruction_prompt": {"key": "review-instructions", "version": "1"},
        "model_policy": {"key": "review-turns", "version": "1"},
        "tools": [{"alias": "lookup", "capability": {"key": "readonly-lookup", "version": "1"}}],
        "limits": {"max_turns": 4, "max_tool_calls": 2, "timeout_seconds": 60},
    }


def test_agent_design_pins_dependencies_and_rejects_endpoint_injection():
    spec = AgentDefinition.model_validate(definition())
    assert [(ref.key, ref.version) for ref in spec.dependency_refs()] == [
        ("review-instructions", "1"), ("review-turns", "1"), ("readonly-lookup", "1"),
    ]
    payload = definition()
    payload["endpoint"] = "https://unreviewed.example.com"
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(payload)


def test_agent_design_rejects_ambiguous_tools_and_implicit_execution_limits():
    payload = definition()
    payload["tools"].append(dict(payload["tools"][0]))
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(payload)
    payload = definition()
    payload["limits"]["max_turns"] = True
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(payload)


def test_call_owner_is_exclusive_and_is_not_a_lease_or_authorization_token():
    owner = TypeAdapter(CallOwner).validate_python({
        "kind": "agent_step", "agent_execution_id": "execution", "step_id": "step",
    })
    assert owner.kind == "agent_step"
    with pytest.raises(ValidationError):
        TypeAdapter(CallOwner).validate_python({
            "kind": "agent_step", "agent_execution_id": "execution", "step_id": "step", "node_id": "fake-node",
        })


def test_runtime_selection_is_explicit_and_does_not_fallback():
    class Runtime:
        descriptor = AgentRuntimeDescriptor(key="pi", profile="enterprise-ai.pi.v1")

        async def check_ready(self):
            raise AssertionError("Selection smoke must not start a runtime")

        async def run(self, start, callbacks):
            raise AssertionError("Selection smoke must not execute a runtime")

    installed = Runtime()
    registry = AgentRuntimeRegistry([installed])
    assert registry.get("pi").adapter is installed
    assert registry.get("not-installed") is None
    with pytest.raises(ValueError, match="Duplicate"):
        AgentRuntimeRegistry([installed, installed])
    with pytest.raises(ValidationError):
        AgentRuntimeDescriptor(key="pi", profile="pi", supports_resume=True)


def test_skills_are_pinned_guidance_not_executable_plugins():
    payload = definition()
    payload["skills"] = [{"key": "evidence-guide", "version": "1"}]
    agent = AgentDefinition.model_validate(payload)
    assert (agent.dependency_refs()[-1].key, agent.dependency_refs()[-1].version) == ("evidence-guide", "1")
    assert SkillDefinition(instructions="Review evidence").instructions == "Review evidence"
    with pytest.raises(ValidationError):
        SkillDefinition(instructions="Review evidence", command="shell")
    payload["skills"].append({"key": "evidence-guide", "version": "2"})
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(payload)
