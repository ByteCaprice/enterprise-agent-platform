"""Pure normalization and exact-ID validation; model text never replaces measurements."""

from domain.task_execution import ExecutionFailure

from .schemas import FinalizeInput, WeatherQueryInput, WeatherQueryOutput


async def normalize_step(_context, values: dict) -> dict:
    query = WeatherQueryInput.model_validate(values)
    result = query.model_dump(mode="json")
    for location in result["locations"]:
        location["name"] = location["name"].strip()
    return WeatherQueryInput.model_validate(result).model_dump(mode="json")


def finalize_weather(values: dict) -> dict:
    args = FinalizeInput.model_validate(values)
    supplied = {location.id: location for location in args.query.locations}
    observations = {observation.location.id: observation for observation in args.observations}
    unavailable = {item.location.id: item for item in args.unavailable}
    summaries = {summary.location_id: summary.text for summary in args.explanation.summaries}
    successful = {key for key, observation in observations.items() if observation.status == "available"}
    if (len(observations) != len(args.observations) or len(unavailable) != len(args.unavailable)
            or len(summaries) != len(args.explanation.summaries) or set(summaries) != successful
            or set(observations) | set(unavailable) != set(supplied)
            or successful & set(unavailable)):
        raise ExecutionFailure("WEATHER_RESULT_INVALID")
    for item in (*args.observations, *args.unavailable):
        if item.location != supplied.get(item.location.id):
            raise ExecutionFailure("WEATHER_RESULT_INVALID")
    for key, item in unavailable.items():
        if item.status == "not_found":
            if key not in observations or observations[key].status != "not_found" or item.error_code is not None:
                raise ExecutionFailure("WEATHER_RESULT_INVALID")
        elif key in observations or item.error_code not in {"TOOL_UPSTREAM_UNAVAILABLE", "TOOL_OUTPUT_INVALID",
                                                           "TOOL_TIMEOUT_RESULT_UNKNOWN", "TOOL_EXECUTION_FAILED_RESULT_UNKNOWN"}:
            raise ExecutionFailure("WEATHER_RESULT_INVALID")
    if any(observation.status == "not_found" and key not in unavailable for key, observation in observations.items()):
        raise ExecutionFailure("WEATHER_RESULT_INVALID")
    locations = []
    for location in args.query.locations:
        observation = observations.get(location.id)
        missing = unavailable.get(location.id)
        locations.append({
            "location": location.model_dump(mode="json"),
            "status": observation.status if observation else missing.status,
            "observation": observation.model_dump(mode="json") if observation else None,
            "summary": summaries.get(location.id), "error_code": missing.error_code if missing else None,
        })
    status = "available" if len(successful) == len(supplied) else "partial" if successful else "unavailable"
    return WeatherQueryOutput.model_validate({"status": status, "locations": locations}).model_dump(mode="json")


async def finalize_step(_context, values: dict) -> dict:
    return finalize_weather(values)
