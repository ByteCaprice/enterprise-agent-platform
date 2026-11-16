"""A Pi Agent whose policy must be explicitly supplied by the operator."""

from domain.capability import CapabilityDependency
from domain.platform_agent import AgentDefinition, AgentLimits, AgentToolBinding

AGENT_KEY = "remitter-search-agent"
PROMPT_KEY = "remitter-search.instructions"
TOOL_KEY = "remitter-search.lookup"
VERSION = "1"


def agent_definition(model_policy: CapabilityDependency) -> AgentDefinition:
    return AgentDefinition(
        definition_version="agent.v1", runtime="pi",
        instruction_prompt={"key": PROMPT_KEY, "version": VERSION},
        model_policy={"key": model_policy.key, "version": model_policy.version},
        tools=(AgentToolBinding(alias="lookup_candidates", capability={"key": TOOL_KEY, "version": VERSION}),),
        limits=AgentLimits(max_turns=4, max_tool_calls=2, timeout_seconds=120.0),
    )
