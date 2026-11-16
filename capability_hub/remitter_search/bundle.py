"""All resources and the complete local business source closure, explicitly pinned."""

from capability_hub.catalog import HubBundle
from domain.business_code import CodeReference
from domain.capability import CapabilityDependency, CapabilitySpec, CapabilityVersionDraft
from domain.capability_bundle import BundleCapability, CapabilityBundle
from domain.platform_tool import InstalledReadonlyTool, ToolDefinition
from framework.business_code import InstalledCodeWorkflow, SourceManifest

from .agent import AGENT_KEY, PROMPT_KEY, TOOL_KEY, VERSION, agent_definition
from .prompts import INSTRUCTIONS
from .schemas import AgentSelection, LookupOutput, SearchInput, SearchOutput
from .steps import normalize_step, validate_selection_step
from .tools import lookup_candidates
from .workflow import NORMALIZE_ACTIVITY, VALIDATE_ACTIVITY, WORKFLOW_KEY, run, workflow_definition

SOURCE = SourceManifest(package="capability_hub.remitter_search", files=(
    "__init__.py", "agent.py", "bundle.py", "prompts.py", "schemas.py", "steps.py", "tools.py", "workflow.py",
))
NAME = "Remitter candidate name search"
DESCRIPTION = ("Read-only name search and selection over candidates supplied by the caller. "
               "No live remitter backend, company policy, identity verification or compliance certification.")


def _build(policy: CapabilityDependency, workflow_reference: CodeReference, tool_reference: CodeReference) -> CapabilityBundle:
    def resource(key, kind, name, definition, *, input_schema=None, output_schema=None, dependencies=()):
        return BundleCapability(
            CapabilitySpec(key=key, kind=kind, name=name, description=DESCRIPTION),
            CapabilityVersionDraft(version=VERSION, definition=definition,
                input_schema=input_schema if input_schema is not None else {"type": "object", "additionalProperties": False},
                output_schema=output_schema if output_schema is not None else {"type": "object"},
                dependencies=[CapabilityDependency(key=key, version=version) for key, version in dependencies]),
        )

    prompt = resource(PROMPT_KEY, "prompt", "Remitter search Agent instructions", {"template": INSTRUCTIONS},
                      output_schema={"type": "string"})
    tool = resource(TOOL_KEY, "tool", "Rank supplied remitter candidates",
        ToolDefinition(definition_version="tool.code.v1", binding_key=TOOL_KEY, operation_type="read_only",
                       timeout_seconds=10.0, implementation=tool_reference).model_dump(mode="json"),
        input_schema=SearchInput.model_json_schema(), output_schema=LookupOutput.model_json_schema())
    agent = resource(AGENT_KEY, "agent", "Remitter search Agent", agent_definition(policy).model_dump(mode="json"),
        input_schema=SearchInput.model_json_schema(), output_schema=AgentSelection.model_json_schema(),
        dependencies=((PROMPT_KEY, VERSION), (TOOL_KEY, VERSION), (policy.key, policy.version)))
    workflow = resource(WORKFLOW_KEY, "workflow", NAME, workflow_definition(workflow_reference).model_dump(mode="json"),
        input_schema=SearchInput.model_json_schema(), output_schema=SearchOutput.model_json_schema(),
        dependencies=((AGENT_KEY, VERSION),))
    return CapabilityBundle(WORKFLOW_KEY, VERSION, NAME, (workflow, agent, tool, prompt),
                            description=DESCRIPTION, references=(workflow_reference, tool_reference))


def bundle_descriptor() -> HubBundle:
    workflow_reference = SOURCE.reference(WORKFLOW_KEY, VERSION)
    tool_reference = SOURCE.reference(TOOL_KEY, VERSION)
    workflow = InstalledCodeWorkflow(reference=workflow_reference, run=run,
        steps={NORMALIZE_ACTIVITY: normalize_step, VALIDATE_ACTIVITY: validate_selection_step}, source=SOURCE)
    tool = InstalledReadonlyTool(TOOL_KEY, VERSION, lookup_candidates, implementation=tool_reference)
    return HubBundle(key=WORKFLOW_KEY, version=VERSION, name=NAME, description=DESCRIPTION,
        resources=((PROMPT_KEY, "prompt"), (TOOL_KEY, "tool"), (AGENT_KEY, "agent"), (WORKFLOW_KEY, "workflow")),
        workflows=(workflow,), tools=(tool,),
        _builder=lambda policy: _build(policy, workflow_reference, tool_reference))
