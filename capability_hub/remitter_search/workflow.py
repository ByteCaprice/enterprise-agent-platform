"""A real mixed code Workflow, with a model-free empty-input branch."""

from domain.business_code import CodeActivity, CodeReference, CodeWorkflowDefinition

from .agent import AGENT_KEY, VERSION

WORKFLOW_KEY = "remitter-search"
NORMALIZE_ACTIVITY = "normalize-input"
AGENT_ACTIVITY = "select-candidates"
VALIDATE_ACTIVITY = "validate-selection"


def workflow_definition(reference: CodeReference) -> CodeWorkflowDefinition:
    return CodeWorkflowDefinition(
        definition_version="workflow.code.v1", implementation=reference,
        activities=(
            CodeActivity(key=NORMALIZE_ACTIVITY, kind="step", required=True),
            CodeActivity(key=AGENT_ACTIVITY, kind="agent", required=False,
                              capability={"key": AGENT_KEY, "version": VERSION}, timeout_seconds=120.0),
            CodeActivity(key=VALIDATE_ACTIVITY, kind="step", required=True),
        ),
    )


async def run(context, values: dict) -> dict:
    search = (await context.call(NORMALIZE_ACTIVITY, values)).require_value()
    if not search["query"] or not search["candidates"]:
        await context.skip(AGENT_ACTIVITY)
        selection = {"selected_candidate_ids": []}
    else:
        selection = (await context.call(AGENT_ACTIVITY, search)).require_value()
    return (await context.call(VALIDATE_ACTIVITY, {"search": search, "selection": selection})).require_value()
