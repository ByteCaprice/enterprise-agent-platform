"""Offline unit checks only: real tool HTTP code with MockTransport, no DB/model."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from capability_hub.catalog import HubCatalog
from capability_hub.weather_query import tools
from capability_hub.weather_query.bundle import SOURCE, bundle_descriptor
from capability_hub.weather_query.schemas import WeatherQueryInput
from capability_hub.weather_query.steps import finalize_weather, normalize_step
from capability_hub.weather_query.workflow import AGENT_ACTIVITY, OBSERVE_ACTIVITY, run
from domain.business_code import ActivityOutcome
from domain.capability import CapabilityDependency
from domain.capability_definitions import interpret_definition
from domain.task_execution import ExecutionFailure
from infra.schema_validation import check_json_schema, validate_json

LOCATION = {"id": "shanghai", "name": "Shanghai", "country_code": "CN"}
GEOCODING = {"results": [{"id": 1796236, "name": "Shanghai", "latitude": 31.22222,
    "longitude": 121.45806, "country_code": "CN", "country": "China", "admin1": "Shanghai",
    "timezone": "Asia/Shanghai", "population": 22315474}], "generationtime_ms": 0.4}
FORECAST = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [121.4581, 31.2222, 5]},
    "properties": {"meta": {"updated_at": "2026-11-05T05:17:39Z", "units": {
        "air_temperature": "celsius", "relative_humidity": "%", "wind_speed": "m/s", "precipitation_amount": "mm"}},
        "timeseries": [{"time": "2026-11-05T06:00:00Z", "data": {
            "instant": {"details": {"air_temperature": 20.1, "relative_humidity": 62.0, "wind_speed": 3.4,
                "cloud_area_fraction": 90.0}},
            "next_1_hours": {"summary": {"symbol_code": "cloudy"}, "details": {"precipitation_amount": 0.0}}}}]}}


def install_http(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        assert kwargs["timeout"].read <= 10
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(tools.httpx, "AsyncClient", client)


def test_explicit_bundle_closure_and_real_schema_validator():
    descriptor = bundle_descriptor()
    catalog = HubCatalog((descriptor,))
    bundle = catalog.build("weather-query", policy=CapabilityDependency(key="operator-policy", version="7"))
    assert len(descriptor.workflows) == 2 and len(descriptor.tools) == 1
    assert len(catalog.references) == 3
    assert set(SOURCE.files) == {path.name for path in Path("capability_hub/weather_query").glob("*.py")}
    assert all(reference == SOURCE.reference(reference.key, reference.version) for reference in catalog.references)
    assert [(item.key, item.version) for item in bundle.external_dependencies] == [("operator-policy", "7")]
    definitions = {item.key: item.draft.definition for item in bundle.capabilities}
    assert definitions["weather-query-agent"]["tools"] == []
    assert definitions["weather-query-agent"]["limits"]["max_tool_calls"] == 0
    parent = definitions["weather-query"]["activities"]
    child_call = next(item for item in parent if item["key"] == OBSERVE_ACTIVITY)
    assert child_call["kind"] == "workflow" and child_call["max_calls"] == 3
    assert child_call["on_unavailable"] == "continue" and child_call["timeout_seconds"] == 40
    assert next(item for item in parent if item["key"] == AGENT_ACTIVITY)["required"] is False
    assert definitions["weather-observation"]["activities"][0]["on_unavailable"] == "fail"
    for item in bundle.capabilities:
        assert interpret_definition(item.kind, item.draft).materialize() is not None
        check_json_schema(item.draft.input_schema)
        check_json_schema(item.draft.output_schema)


def test_fixed_real_http_contract_and_unchanged_measurements(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=GEOCODING if len(requests) == 1 else FORECAST)

    install_http(monkeypatch, handler)
    result = asyncio.run(tools.fetch_current_weather(None, LOCATION))
    assert [str(request.url).split("?")[0] for request in requests] == [tools.GEOCODING_URL, tools.FORECAST_URL]
    assert dict(requests[0].url.params) == {"name": "Shanghai", "count": "1", "language": "en", "format": "json", "countryCode": "CN"}
    params = dict(requests[1].url.params)
    assert params == {"lat": "31.2222", "lon": "121.4581"}
    point = FORECAST["properties"]["timeseries"][0]
    for key in ("air_temperature", "relative_humidity", "wind_speed"):
        assert result["forecast"][key] == point["data"]["instant"]["details"][key]
    assert result["forecast"]["time"] == point["time"]
    assert result["forecast"]["precipitation_amount"] == 0.0
    assert result["forecast"]["precipitation_period"] == "next_1_hours"
    assert result["forecast"]["symbol_code"] == "cloudy"
    assert result["units"] == FORECAST["properties"]["meta"]["units"]
    assert result["provider_updated_at"] == FORECAST["properties"]["meta"]["updated_at"]
    assert result["resolved"]["selection"] == "first_provider_result"
    assert result["data_kind"] == "near_term_forecast" and result["source"] == "MET Norway"
    assert result["retrieved_at"].endswith("+00:00")
    args = {"query": {"locations": [LOCATION]}, "observations": [result], "unavailable": [],
            "explanation": {"summaries": [{"location_id": "shanghai", "text": "近期模型预报，非气象站实测。"}]}}
    final = finalize_weather(args)
    assert final["status"] == "available" and final["locations"][0]["observation"] == result
    validate_json(final, next(item for item in bundle_descriptor().build(
        policy=CapabilityDependency(key="operator-policy", version="7")).capabilities if item.key == "weather-query").draft.output_schema)
    for ids in ([], ["beijing"], ["shanghai", "shanghai"]):
        changed = copy.deepcopy(args)
        changed["explanation"] = {"summaries": [{"location_id": key, "text": "天气"} for key in ids]}
        with pytest.raises(ExecutionFailure, match="WEATHER_RESULT_INVALID"):
            finalize_weather(changed)


@pytest.mark.parametrize("case,code", [("503", "TOOL_UPSTREAM_UNAVAILABLE"), ("redirect", "TOOL_UPSTREAM_UNAVAILABLE"),
    ("401", "TOOL_UPSTREAM_AUTHORIZATION_FAILED"), ("connect", "TOOL_UPSTREAM_UNAVAILABLE"),
    ("json", "TOOL_OUTPUT_INVALID"), ("oversize", "TOOL_OUTPUT_INVALID"),
    ("null", "TOOL_OUTPUT_INVALID"), ("country", "TOOL_OUTPUT_INVALID"), ("nan", "TOOL_OUTPUT_INVALID"),
    ("malformed-geocoding", "TOOL_OUTPUT_INVALID")])
def test_safe_upstream_failures_do_not_become_weather(monkeypatch, case, code):
    requests = []

    def handler(request):
        requests.append(request)
        if case == "connect":
            raise httpx.ConnectError("private upstream details", request=request)
        if case in {"503", "401"}:
            return httpx.Response(int(case))
        if case == "redirect":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        if case == "json":
            return httpx.Response(200, content=b"not json")
        if case == "oversize":
            return httpx.Response(200, content=b" " * (tools.MAX_RESPONSE_BYTES + 1))
        if case == "malformed-geocoding":
            return httpx.Response(200, json={"unexpected": "payload"})
        geo = copy.deepcopy(GEOCODING)
        if case == "country":
            geo["results"][0]["country_code"] = "US"
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=geo)
        forecast = copy.deepcopy(FORECAST)
        forecast["properties"]["timeseries"][0]["data"]["instant"]["details"]["air_temperature"] = None if case == "null" else float("nan")
        # Deliberately malformed wire JSON; HTTPX's json= rejects NaN itself.
        return httpx.Response(200, content=json.dumps(forecast).encode())

    install_http(monkeypatch, handler)
    with pytest.raises(ExecutionFailure) as error:
        asyncio.run(tools.fetch_current_weather(None, LOCATION))
    assert error.value.code == code and "private" not in str(error.value)
    assert all(request.url.host in {"geocoding-api.open-meteo.com", "api.met.no"} for request in requests)


def test_not_found_and_all_unavailable_skip_model(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"generationtime_ms": 0.2})

    install_http(monkeypatch, handler)
    missing = asyncio.run(tools.fetch_current_weather(None, LOCATION))
    assert missing["status"] == "not_found" and missing["forecast"] is None and len(requests) == 1
    for outcome in (ActivityOutcome("succeeded", missing),
                    ActivityOutcome("unavailable", error_code="TOOL_UPSTREAM_UNAVAILABLE")):
        async def call(activity, values):
            if activity == "normalize-input":
                return ActivityOutcome("succeeded", await normalize_step(None, values))
            assert activity == "finalize-weather"  # Any model activity fails this unit assertion.
            return ActivityOutcome("succeeded", finalize_weather(values))

        context = SimpleNamespace(call=call, map=AsyncMock(return_value=[outcome]), skip=AsyncMock())
        result = asyncio.run(run(context, {"locations": [LOCATION]}))
        assert result["status"] == "unavailable" and result["locations"][0]["summary"] is None
        context.skip.assert_awaited_once_with(AGENT_ACTIVITY)
        assert context.map.call_args.kwargs["concurrency"] == 2
        assert context.map.call_args.kwargs["key"](LOCATION) == "shanghai"


def test_optional_next_hour_and_partial_result_keep_raw_forecast(monkeypatch):
    forecast = copy.deepcopy(FORECAST)
    del forecast["properties"]["timeseries"][0]["data"]["next_1_hours"]
    install_http(monkeypatch, lambda request: httpx.Response(200, json=(
        GEOCODING if request.url.host == "geocoding-api.open-meteo.com" else forecast)))
    available = asyncio.run(tools.fetch_current_weather(None, LOCATION))
    assert available["forecast"]["symbol_code"] is None
    assert available["forecast"]["precipitation_amount"] is None
    assert available["forecast"]["precipitation_period"] is None
    beijing = {"id": "beijing", "name": "Beijing", "country_code": "CN"}
    absent = {"id": "absent", "name": "No matching city", "country_code": "CN"}
    not_found = {"location": absent, "status": "not_found", "retrieved_at": available["retrieved_at"]}
    args = {"query": {"locations": [LOCATION, beijing, absent]}, "observations": [available, not_found],
        "unavailable": [{"location": beijing, "status": "unavailable", "error_code": "TOOL_UPSTREAM_UNAVAILABLE"},
                        {"location": absent, "status": "not_found"}],
        "explanation": {"summaries": [{"location_id": "shanghai", "text": "近期模型预报，降水信息未知。"}]}}
    result = finalize_weather(args)
    assert result["status"] == "partial"
    assert [item["status"] for item in result["locations"]] == ["available", "unavailable", "not_found"]
    assert result["locations"][0]["observation"] == available
    assert all(item["summary"] is None for item in result["locations"][1:])


def test_operator_user_agent_and_unexpected_failures_are_not_swallowed(monkeypatch):
    monkeypatch.setenv("WEATHER_QUERY_USER_AGENT", "WeatherDemo/1.0 https://operator.example/contact")

    def handler(request):
        assert request.headers["User-Agent"] == "WeatherDemo/1.0 https://operator.example/contact"
        raise RuntimeError("unexpected implementation failure")

    install_http(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="unexpected implementation failure"):
        asyncio.run(tools.fetch_current_weather(None, LOCATION))
    monkeypatch.setenv("WEATHER_QUERY_USER_AGENT", "bad\nheader")
    with pytest.raises(ExecutionFailure, match="TOOL_CONFIGURATION_INVALID"):
        asyncio.run(tools.fetch_current_weather(None, LOCATION))


def test_strict_bounded_inputs():
    for values in ({"locations": []}, {"locations": [LOCATION] * 2}, {"locations": [LOCATION] * 4},
                   {"locations": [{**LOCATION, "id": "bad.key"}]},
                   {"locations": [{**LOCATION, "country_code": "cn"}]},
                   {"locations": [{**LOCATION, "url": "https://attacker.invalid"}]}):
        with pytest.raises(ValidationError):
            WeatherQueryInput.model_validate(values)
    with pytest.raises(ValidationError):
        asyncio.run(normalize_step(None, {"locations": [{**LOCATION, "name": "   "}]}))
