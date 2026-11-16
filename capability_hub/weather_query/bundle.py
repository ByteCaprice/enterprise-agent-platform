"""Explicit Hub descriptor: two code Workflows, one read-only tool and Pi Agent."""

from capability_hub.catalog import HubBundle
from domain.business_code import CodeReference
from domain.capability import CapabilityDependency, CapabilitySpec, CapabilityVersionDraft
from domain.capability_bundle import BundleCapability, CapabilityBundle
from domain.platform_tool import InstalledReadonlyTool, ToolDefinition
from framework.business_code import InstalledCodeWorkflow, SourceManifest

from .agent import AGENT_KEY, PROMPT_KEY, TOOL_KEY, VERSION, agent_definition
from .prompts import INSTRUCTIONS
from .schemas import AgentInput, AgentSummaries, Location, Observation, WeatherQueryInput, WeatherQueryOutput
from .steps import finalize_step, normalize_step
from .tools import fetch_current_weather
from .workflow import (
    FINALIZE_ACTIVITY,
    NORMALIZE_ACTIVITY,
    OBSERVATION_WORKFLOW_KEY,
    WORKFLOW_KEY,
    observation_workflow_definition,
    observe,
    run,
    workflow_definition,
)

SOURCE = SourceManifest(package="capability_hub.weather_query", files=(
    "__init__.py", "agent.py", "bundle.py", "prompts.py", "schemas.py", "steps.py", "tools.py", "workflow.py",
))
NAME = "Near-term weather forecast query"
DESCRIPTION = ("MET Norway near-term forecast for 1–3 locations; Open-Meteo first-result geocoding, optional country pin, "
               "bounded read-only HTTP and grounded Chinese Pi explanations. Not station observations. "
               "Retain MET Norway and Open-Meteo/GeoNames attribution. Set WEATHER_QUERY_USER_AGENT with real operator contact for ongoing use. "
               "Provider, model policy and execution grants remain operator-owned.")


def _build(policy: CapabilityDependency, root: CodeReference, child: CodeReference, tool: CodeReference) -> CapabilityBundle:
    def resource(key, kind, name, definition, *, input_schema=None, output_schema=None, dependencies=()):
        return BundleCapability(
            CapabilitySpec(key=key, kind=kind, name=name, description=DESCRIPTION),
            CapabilityVersionDraft(
                version=VERSION, definition=definition,
                input_schema=input_schema if input_schema is not None else {"type": "object", "additionalProperties": False},
                output_schema=output_schema if output_schema is not None else {"type": "object"},
                dependencies=[CapabilityDependency(key=key, version=version) for key, version in dependencies],
            ),
        )

    prompt = resource(PROMPT_KEY, "prompt", "Grounded weather explanation instructions", {"template": INSTRUCTIONS},
                      output_schema={"type": "string"})
    current = resource(TOOL_KEY, "tool", "Fetch MET Norway near-term forecast",
        ToolDefinition(definition_version="tool.code.v1", binding_key=TOOL_KEY, operation_type="read_only",
                       timeout_seconds=30.0, implementation=tool).model_dump(mode="json"),
        input_schema=Location.model_json_schema(), output_schema=Observation.model_json_schema())
    observation = resource(OBSERVATION_WORKFLOW_KEY, "workflow", "Observe one weather location",
        observation_workflow_definition(child).model_dump(mode="json"),
        input_schema=Location.model_json_schema(), output_schema=Observation.model_json_schema(),
        dependencies=((TOOL_KEY, VERSION),))
    agent = resource(AGENT_KEY, "agent", "Explain supplied weather in Chinese",
        agent_definition(policy).model_dump(mode="json"),
        input_schema=AgentInput.model_json_schema(), output_schema=AgentSummaries.model_json_schema(),
        dependencies=((PROMPT_KEY, VERSION), (policy.key, policy.version)))
    workflow = resource(WORKFLOW_KEY, "workflow", NAME, workflow_definition(root).model_dump(mode="json"),
        input_schema=WeatherQueryInput.model_json_schema(), output_schema=WeatherQueryOutput.model_json_schema(),
        dependencies=((AGENT_KEY, VERSION), (OBSERVATION_WORKFLOW_KEY, VERSION)))
    return CapabilityBundle(WORKFLOW_KEY, VERSION, NAME, (workflow, observation, agent, current, prompt),
                            description=DESCRIPTION, references=(root, child, tool))


def bundle_descriptor() -> HubBundle:
    root = SOURCE.reference(WORKFLOW_KEY, VERSION)
    child = SOURCE.reference(OBSERVATION_WORKFLOW_KEY, VERSION)
    tool = SOURCE.reference(TOOL_KEY, VERSION)
    workflows = (
        InstalledCodeWorkflow(reference=root, run=run,
            steps={NORMALIZE_ACTIVITY: normalize_step, FINALIZE_ACTIVITY: finalize_step}, source=SOURCE),
        InstalledCodeWorkflow(reference=child, run=observe, source=SOURCE),
    )
    return HubBundle(
        key=WORKFLOW_KEY, version=VERSION, name=NAME, description=DESCRIPTION,
        resources=((PROMPT_KEY, "prompt"), (TOOL_KEY, "tool"), (AGENT_KEY, "agent"),
                   (OBSERVATION_WORKFLOW_KEY, "workflow"), (WORKFLOW_KEY, "workflow")),
        workflows=workflows, tools=(InstalledReadonlyTool(TOOL_KEY, VERSION, fetch_current_weather, implementation=tool),),
        _builder=lambda policy: _build(policy, root, child, tool),
    )
