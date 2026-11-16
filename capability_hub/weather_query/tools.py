"""Fixed-origin Open-Meteo geocoding + MET Norway compact forecast, no fallback.

References: https://open-meteo.com/en/docs/geocoding-api and
https://docs.api.met.no/doc/ForecastJSON. Set WEATHER_QUERY_USER_AGENT with a
genuine application identifier and operator contact for ongoing MET Norway use.
No private configuration, credentials, caller URLs, retries or ambient proxies.
"""

import asyncio
import json
import os
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from domain.platform_tool import ToolContext
from domain.task_execution import ExecutionFailure

from .schemas import ForecastPoint, ForecastUnits, Location, Observation, ResolvedLocation

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
MAX_RESPONSE_BYTES = 128 * 1024
DEVELOPMENT_USER_AGENT = "enterprise-ai-platform-weather-demo/0.1 local-development-validation"


class _ProviderObject(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)


class _Geocoding(_ProviderObject):
    generationtime_ms: float = Field(ge=0)
    results: list[dict] = Field(default_factory=list, max_length=100)


class _InstantDetails(_ProviderObject):
    air_temperature: float = Field(ge=-150, le=100)
    relative_humidity: float = Field(ge=0, le=100)
    wind_speed: float = Field(ge=0, le=300)


class _Instant(_ProviderObject):
    details: _InstantDetails


class _Summary(_ProviderObject):
    symbol_code: str | None = Field(default=None, min_length=1, max_length=100)


class _Precipitation(_ProviderObject):
    precipitation_amount: float | None = Field(default=None, ge=0, le=10000)


class _NextHour(_ProviderObject):
    summary: _Summary | None = None
    details: _Precipitation | None = None


class _Data(_ProviderObject):
    instant: _Instant
    next_1_hours: _NextHour | None = None


class _TimePoint(_ProviderObject):
    time: str = Field(max_length=40, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    data: _Data


class _Meta(_ProviderObject):
    updated_at: str = Field(max_length=40, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    units: dict


class _Properties(_ProviderObject):
    meta: _Meta
    timeseries: list[_TimePoint] = Field(min_length=1, max_length=300)


class _Forecast(_ProviderObject):
    properties: _Properties


def _reject_constant(_value):
    raise ValueError("Non-finite provider JSON")


async def _read_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    async with client.stream("GET", url, params=params) as response:
        if response.status_code in {401, 403}:
            raise ExecutionFailure("TOOL_UPSTREAM_AUTHORIZATION_FAILED")
        if response.status_code != 200:
            raise ExecutionFailure("TOOL_UPSTREAM_UNAVAILABLE")
        # Identity encoding also avoids decompression of unbounded compressed bodies.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ExecutionFailure("TOOL_OUTPUT_INVALID")
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ExecutionFailure("TOOL_OUTPUT_INVALID")
            body.extend(chunk)
        try:
            payload = json.loads(body, parse_constant=_reject_constant)
        except (ValueError, UnicodeError):
            raise ExecutionFailure("TOOL_OUTPUT_INVALID") from None
        if not isinstance(payload, dict) or payload.get("error"):
            raise ExecutionFailure("TOOL_OUTPUT_INVALID")
        return payload


async def _fetch(location: Location, user_agent: str) -> dict:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0, pool=5.0),
        follow_redirects=False, trust_env=False,
        headers={"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": user_agent},
    ) as client:
        params = {"name": location.name, "count": 1, "language": "en", "format": "json"}
        if location.country_code is not None:
            params["countryCode"] = location.country_code
        geocoding = _Geocoding.model_validate(await _read_json(client, GEOCODING_URL, params))
        if not geocoding.results:
            return Observation(location=location, status="not_found", retrieved_at=datetime.now(UTC).isoformat()).model_dump(mode="json")
        first = geocoding.results[0]
        resolved = ResolvedLocation.model_validate({
            key: first.get(key) for key in ("name", "country_code", "country", "admin1", "latitude", "longitude")
        })
        if location.country_code is not None and resolved.country_code != location.country_code:
            raise ExecutionFailure("TOOL_OUTPUT_INVALID")
        lat, lon = f"{resolved.latitude:.4f}", f"{resolved.longitude:.4f}"
        forecast = _Forecast.model_validate(await _read_json(client, FORECAST_URL, {"lat": lat, "lon": lon}))
        properties = forecast.properties
        first_point = properties.timeseries[0]
        next_hour = first_point.data.next_1_hours
        precipitation = next_hour.details.precipitation_amount if next_hour and next_hour.details else None
        point = ForecastPoint(
            time=first_point.time, **first_point.data.instant.details.model_dump(),
            precipitation_amount=precipitation,
            precipitation_period="next_1_hours" if precipitation is not None else None,
            symbol_code=next_hour.summary.symbol_code if next_hour and next_hour.summary else None,
        )
        units = ForecastUnits.model_validate({
            key: properties.meta.units.get(key) for key in ("air_temperature", "relative_humidity", "wind_speed", "precipitation_amount")
        })
        return Observation(
            location=location, status="available", resolved=resolved, forecast=point, units=units,
            provider_updated_at=properties.meta.updated_at, forecast_latitude=float(lat), forecast_longitude=float(lon),
            retrieved_at=datetime.now(UTC).isoformat(),
        ).model_dump(mode="json")


async def fetch_current_weather(_context: ToolContext, values: dict) -> dict:
    """The weather-query.current capability explicitly returns a near-term forecast."""
    location = Location.model_validate(values)
    user_agent = os.environ.get("WEATHER_QUERY_USER_AGENT", DEVELOPMENT_USER_AGENT)
    if not 1 <= len(user_agent) <= 500 or any(ord(char) < 32 or ord(char) > 126 for char in user_agent):
        raise ExecutionFailure("TOOL_CONFIGURATION_INVALID")
    try:
        # Total deadline bounds even a server trickling chunks below read timeout.
        async with asyncio.timeout(25.0):
            return await _fetch(location, user_agent)
    except (TimeoutError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
        raise ExecutionFailure("TOOL_UPSTREAM_UNAVAILABLE") from None
    except (ValidationError, httpx.DecodingError):
        raise ExecutionFailure("TOOL_OUTPUT_INVALID") from None
