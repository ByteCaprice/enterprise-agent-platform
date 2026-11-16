"""Strict, finite, bounded public JSON contracts for the complete weather flow."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LocationId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
CityName = Annotated[str, Field(min_length=1, max_length=200)]
CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
Timestamp = Annotated[str, Field(min_length=1, max_length=40)]
SummaryText = Annotated[str, Field(min_length=1, max_length=800)]
ATTRIBUTION = ("Forecast: MET Norway (https://www.met.no/), CC BY 4.0; "
               "geocoding: Open-Meteo (https://open-meteo.com/), GeoNames. "
               "Near-term model forecast, not current station observations.")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Location(Contract):
    id: LocationId
    name: CityName
    country_code: CountryCode | None = None


class WeatherQueryInput(Contract):
    locations: list[Location] = Field(min_length=1, max_length=3)
    language: Literal["zh-CN"] = "zh-CN"

    @model_validator(mode="after")
    def unique_ids(self):
        if len({location.id for location in self.locations}) != len(self.locations):
            raise ValueError("Location IDs must be unique")
        return self


class ResolvedLocation(Contract):
    name: CityName
    country_code: CountryCode
    country: str | None = Field(default=None, max_length=200)
    admin1: str | None = Field(default=None, max_length=200)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    selection: Literal["first_provider_result"] = "first_provider_result"


class ForecastPoint(Contract):
    time: str = Field(max_length=40, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    air_temperature: float = Field(ge=-150, le=100)
    relative_humidity: float = Field(ge=0, le=100)
    wind_speed: float = Field(ge=0, le=300)
    precipitation_amount: float | None = Field(default=None, ge=0, le=10000)
    precipitation_period: Literal["next_1_hours"] | None = None
    symbol_code: str | None = Field(default=None, min_length=1, max_length=100)


class ForecastUnits(Contract):
    air_temperature: Literal["celsius"]
    relative_humidity: Literal["%"]
    wind_speed: Literal["m/s"]
    precipitation_amount: Literal["mm"] | None = None


class Observation(Contract):
    location: Location
    status: Literal["available", "not_found"]
    resolved: ResolvedLocation | None = None
    forecast: ForecastPoint | None = None
    units: ForecastUnits | None = None
    provider_updated_at: str | None = Field(default=None, max_length=40,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    forecast_latitude: float | None = Field(default=None, ge=-90, le=90)
    forecast_longitude: float | None = Field(default=None, ge=-180, le=180)
    retrieved_at: Timestamp
    source: Literal["MET Norway"] = "MET Norway"
    geocoding_source: Literal["Open-Meteo"] = "Open-Meteo"
    data_kind: Literal["near_term_forecast"] = "near_term_forecast"
    forecast_selection: Literal["first_provider_timeseries"] = "first_provider_timeseries"

    @model_validator(mode="after")
    def consistent_status(self):
        fields = (self.resolved, self.forecast, self.units, self.provider_updated_at,
                  self.forecast_latitude, self.forecast_longitude)
        if self.status == "available" and any(value is None for value in fields):
            raise ValueError("Available observations require measurements and provenance")
        if self.status == "not_found" and any(value is not None for value in fields):
            raise ValueError("Not-found results cannot contain weather")
        if self.forecast is not None and self.forecast.precipitation_amount is not None:
            if self.forecast.precipitation_period != "next_1_hours" or self.units.precipitation_amount != "mm":
                raise ValueError("Precipitation requires its provider units and forecast period")
        return self


class UnavailableLocation(Contract):
    location: Location
    status: Literal["unavailable", "not_found"]
    error_code: str | None = Field(default=None, max_length=100)


class AgentInput(Contract):
    observations: list[Observation] = Field(min_length=1, max_length=3)
    unavailable: list[UnavailableLocation] = Field(max_length=3)
    language: Literal["zh-CN"] = "zh-CN"


class LocationSummary(Contract):
    location_id: LocationId
    text: SummaryText


class AgentSummaries(Contract):
    summaries: list[LocationSummary] = Field(max_length=3)


class LocationResult(Contract):
    location: Location
    status: Literal["available", "not_found", "unavailable"]
    observation: Observation | None = None
    summary: SummaryText | None = None
    error_code: str | None = Field(default=None, max_length=100)


class WeatherQueryOutput(Contract):
    status: Literal["available", "partial", "unavailable"]
    locations: list[LocationResult] = Field(min_length=1, max_length=3)
    attribution: Literal[ATTRIBUTION] = ATTRIBUTION


class FinalizeInput(Contract):
    query: WeatherQueryInput
    observations: list[Observation] = Field(max_length=3)
    unavailable: list[UnavailableLocation] = Field(max_length=3)
    explanation: AgentSummaries
