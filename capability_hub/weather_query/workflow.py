"""Parent bounded map -> optional Agent -> pure merge; child tool failures propagate."""

from domain.business_code import CodeActivity, CodeReference, CodeWorkflowDefinition

from .agent import AGENT_KEY, TOOL_KEY, VERSION

WORKFLOW_KEY = "weather-query"
OBSERVATION_WORKFLOW_KEY = "weather-observation"
NORMALIZE_ACTIVITY = "normalize-input"
OBSERVE_ACTIVITY = "observe-location"
FETCH_ACTIVITY = "fetch-weather"
AGENT_ACTIVITY = "explain-weather"
FINALIZE_ACTIVITY = "finalize-weather"


def workflow_definition(reference: CodeReference) -> CodeWorkflowDefinition:
    return CodeWorkflowDefinition(
        definition_version="workflow.code.v1", implementation=reference, max_parallelism=2,
        activities=(
            CodeActivity(key=NORMALIZE_ACTIVITY, kind="step"),
            CodeActivity(key=OBSERVE_ACTIVITY, kind="workflow", max_calls=3, timeout_seconds=40.0,
                         on_unavailable="continue", capability={"key": OBSERVATION_WORKFLOW_KEY, "version": VERSION}),
            CodeActivity(key=AGENT_ACTIVITY, kind="agent", required=False, timeout_seconds=120.0,
                         capability={"key": AGENT_KEY, "version": VERSION}),
            CodeActivity(key=FINALIZE_ACTIVITY, kind="step"),
        ),
    )


def observation_workflow_definition(reference: CodeReference) -> CodeWorkflowDefinition:
    return CodeWorkflowDefinition(
        definition_version="workflow.code.v1", implementation=reference,
        activities=(CodeActivity(key=FETCH_ACTIVITY, kind="tool", timeout_seconds=35.0,
                                 capability={"key": TOOL_KEY, "version": VERSION}),),
    )


async def observe(context, values: dict) -> dict:
    return (await context.call(FETCH_ACTIVITY, values)).require_value()


async def run(context, values: dict) -> dict:
    query = (await context.call(NORMALIZE_ACTIVITY, values)).require_value()
    outcomes = await context.map(OBSERVE_ACTIVITY, query["locations"], key=lambda value: value["id"], concurrency=2)
    observations, unavailable = [], []
    for location, outcome in zip(query["locations"], outcomes, strict=True):
        if outcome.status == "unavailable":
            unavailable.append({"location": location, "status": "unavailable", "error_code": outcome.error_code})
        else:
            observation = outcome.require_value()
            observations.append(observation)
            if observation["status"] == "not_found":
                unavailable.append({"location": location, "status": "not_found", "error_code": None})
    available = [observation for observation in observations if observation["status"] == "available"]
    if available:
        explanation = (await context.call(AGENT_ACTIVITY, {
            "observations": available, "unavailable": unavailable, "language": query["language"],
        })).require_value()
    else:
        await context.skip(AGENT_ACTIVITY)
        explanation = {"summaries": []}
    return (await context.call(FINALIZE_ACTIVITY, {
        "query": query, "observations": observations, "unavailable": unavailable, "explanation": explanation,
    })).require_value()
