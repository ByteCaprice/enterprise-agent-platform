"""A tool-free Pi Agent under an explicitly supplied operator model policy."""

from domain.capability import CapabilityDependency
from domain.platform_agent import AgentDefinition, AgentLimits

VERSION = "1"
AGENT_KEY = "weather-query-agent"
PROMPT_KEY = "weather-query.instructions"
TOOL_KEY = "weather-query.current"


def agent_definition(model_policy: CapabilityDependency) -> AgentDefinition:
    return AgentDefinition(
        definition_version="agent.v1", runtime="pi",
        instruction_prompt={"key": PROMPT_KEY, "version": VERSION},
        model_policy={"key": model_policy.key, "version": model_policy.version},
        tools=(), limits=AgentLimits(max_turns=2, max_tool_calls=0, timeout_seconds=120.0),
    )
