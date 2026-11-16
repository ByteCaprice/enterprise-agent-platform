"""Safe outbound evidence, not a remote exactly-once or billing guarantee.

Dispatch, response receipt, answer acceptance and reported usage are independent.
Unknown dispatch is deliberately not permission to replay a paid operation.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: int | None = Field(default=None, strict=True, ge=0, le=9_223_372_036_854_775_807)
    completion_tokens: int | None = Field(default=None, strict=True, ge=0, le=9_223_372_036_854_775_807)
    total_tokens: int | None = Field(default=None, strict=True, ge=0, le=9_223_372_036_854_775_807)


class OutboundEvidence(BaseModel):
    """Allowlisted immutable metadata only; never a provider body or credential.

    Usage is provider-reported evidence, not independently verified cost. A known
    rejection can permit bounded fallback without implying known zero charges.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dispatch: Literal["not_dispatched", "unknown", "dispatched"] = "unknown"
    response: Literal["not_received", "headers_received", "received"] = "not_received"
    acceptance: Literal["unvalidated", "accepted", "rejected"] = "unvalidated"
    usage: ModelUsage = Field(default_factory=ModelUsage)
    actual_model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,199}$")
    http_status: int | None = Field(default=None, strict=True, ge=100, le=599)
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,200}$")
    retry_permission: Literal["never", "safe_response"] = "never"

    @property
    def usage_certainty(self) -> Literal["unknown", "partial", "reported"]:
        if self.usage.total_tokens is not None or (self.usage.prompt_tokens is not None and
                                                   self.usage.completion_tokens is not None):
            return "reported"
        return "partial" if any(value is not None for value in self.usage.model_dump().values()) else "unknown"

    @model_validator(mode="after")
    def coherent_evidence(self):
        if self.response != "not_received" and (self.dispatch != "dispatched" or self.http_status is None):
            raise ValueError("Response evidence requires dispatch and HTTP status")
        if self.response == "not_received" and (self.http_status is not None or self.request_id is not None):
            raise ValueError("HTTP metadata requires response evidence")
        if self.acceptance == "accepted" and self.response != "received":
            raise ValueError("Acceptance requires a complete response")
        if self.retry_permission == "safe_response" and not (
            self.response == "received" and self.acceptance == "rejected" and
            self.http_status == 429 and self.usage_certainty == "unknown"
        ):
            raise ValueError("Only explicit rate rejection without reported usage permits fallback")
        return self

    def with_acceptance(self, accepted: bool):
        return OutboundEvidence(**{**self.model_dump(), "acceptance": "accepted" if accepted else "rejected"})


def evidence_from_exception(error: BaseException) -> OutboundEvidence:
    """Retain safe progress when an outer deadline converts cancellation to timeout."""
    for _ in range(8):
        evidence = getattr(error, "evidence", None)
        if isinstance(evidence, OutboundEvidence):
            return evidence
        if error.__cause__ is None:
            break
        error = error.__cause__
    return OutboundEvidence()
