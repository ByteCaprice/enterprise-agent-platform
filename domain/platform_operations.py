"""V1 operations contracts. No inferred company identity, pricing or retention policy."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from domain.capability import Key, Version
from domain.platform_model import normalize_provider_url
from domain.task import IdempotencyKey, canonical_json


class OperationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded(self):
        canonical_json(self.model_dump(mode="json"))
        return self


class ProjectLimits(OperationsInput):
    max_queued: int | None = Field(default=None, strict=True, ge=0, le=100000)
    max_running: int | None = Field(default=None, strict=True, ge=0, le=10000)
    active_budget_key: Key | None = None


class ConcurrencyLimit(OperationsInput):
    """None means no configured limit; zero pauses new claims."""

    max_running: int | None = Field(default=None, strict=True, ge=0, le=100000)


class BudgetRate(OperationsInput):
    provider_key: Key
    model: str = Field(min_length=1, max_length=200)
    input_microunits_per_million: int = Field(strict=True, ge=0, le=10**15)
    output_microunits_per_million: int = Field(strict=True, ge=0, le=10**15)


class BudgetCreate(OperationsInput):
    key: Key
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    limit_microunits: int = Field(strict=True, ge=1, le=10**15)
    reserve_per_call_microunits: int = Field(strict=True, ge=1, le=10**15)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    rates: list[BudgetRate] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def ordered(self):
        if self.ends_at <= self.starts_at or len({(r.provider_key, r.model) for r in self.rates}) != len(self.rates):
            raise ValueError("Use an explicit ordered budget window and unique model prices")
        return self


class ChargeReconcile(OperationsInput):
    amount_microunits: int = Field(strict=True, ge=0, le=10**15)
    reference: str = Field(min_length=1, max_length=200)


class CallbackCreate(OperationsInput):
    key: Key
    url: str = Field(max_length=2048)
    credential_ref: Key
    max_attempts: int = Field(strict=True, ge=1, le=20)
    retry_delay_seconds: int = Field(strict=True, ge=1, le=86400)
    timeout_seconds: int = Field(strict=True, ge=1, le=30)

    @field_validator("url")
    @classmethod
    def public_https(cls, value):
        return normalize_provider_url(value)


class EvaluationCase(OperationsInput):
    key: Key
    input: dict[str, JsonValue]
    expected: dict[str, JsonValue]
    matcher: Literal["exact", "contains", "schema"]


class DatasetCreate(OperationsInput):
    key: Key
    version: Version
    cases: list[EvaluationCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.key for case in self.cases}) != len(self.cases):
            raise ValueError("Dataset case keys must be unique")
        return self


class EvaluationCreate(OperationsInput):
    request_key: IdempotencyKey
    dataset_key: Key
    dataset_version: Version
    capability_key: Key
    capability_version: Version


class ReviewCreate(OperationsInput):
    verdict: Literal["approved", "rejected", "needs_review"]
    comment: str = Field(default="", max_length=2000)


class FeedbackCreate(OperationsInput):
    """A submitter's explicit business decision, independent of technical quality."""

    request_key: IdempotencyKey = Field(repr=False)
    expected_revision: int = Field(strict=True, ge=0, le=1_000_000)
    adoption: Literal["adopted", "modified", "not_adopted"]
    final_result: dict[str, JsonValue] | None = Field(default=None, repr=False)
    reason: str = Field(default="", max_length=2000, repr=False)

    @model_validator(mode="after")
    def consistent_outcome(self):
        if (self.adoption == "modified") != (self.final_result is not None):
            raise ValueError("Only modified adoption carries an explicit final result")
        if self.final_result is not None and len(canonical_json(self.final_result).encode()) > 65536:
            raise ValueError("Feedback final result exceeds the size limit")
        return self


class RecoveryCreate(OperationsInput):
    request_key: IdempotencyKey


class MemoryWrite(OperationsInput):
    capability_key: Key
    subject_key: Key
    fact_key: Key
    expected_revision: int = Field(strict=True, ge=0)
    value: JsonValue = None
    state: Literal["active", "deleted"] = "active"
    source_task_id: str | None = Field(default=None, min_length=1, max_length=200)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def tombstone(self):
        if self.state == "deleted" and self.value is not None:
            raise ValueError("Deleted memory must not carry a value")
        return self


class OIDCProviderCreate(OperationsInput):
    key: Key
    issuer: str = Field(min_length=1, max_length=2048)
    audience: str = Field(min_length=1, max_length=300)
    jwks: dict = Field(repr=False)

    @field_validator("issuer")
    @classmethod
    def issuer_https(cls, value):
        # Validate without rewriting issuer: JWT issuer matching is exact.
        normalize_provider_url(value)
        return value

    @field_validator("jwks")
    @classmethod
    def public_keys_only(cls, value):
        keys = value.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 10:
            raise ValueError("Install an explicit public RSA JWKS")
        ids = set()
        for key in keys:
            if (not isinstance(key, dict) or key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256" or
                    key.get("use", "sig") != "sig" or key.get("key_ops", ["verify"]) != ["verify"] or
                    not isinstance(key.get("kid"), str) or not 1 <= len(key["kid"]) <= 200 or
                    key["kid"] in ids or set(key) & {"d", "p", "q", "dp", "dq", "qi", "k"}):
                raise ValueError("Only distinct RS256 public signing keys are allowed")
            ids.add(key["kid"])
        return value


class OIDCBindingCreate(OperationsInput):
    provider_key: Key
    subject: str = Field(min_length=1, max_length=300)
    identity_id: str = Field(min_length=1, max_length=200)
