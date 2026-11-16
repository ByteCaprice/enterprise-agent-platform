"""Bounded public schemas shared by code, publication, and the read-only tool."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CandidateId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[^\s\x00]+$")]
CandidateName = Annotated[str, Field(min_length=1, max_length=300)]
NOTICE = "Name similarity only; not identity verification or compliance certification."


class BusinessInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Candidate(BusinessInput):
    id: CandidateId
    name: CandidateName
    aliases: list[CandidateName] = Field(default_factory=list, max_length=8)


class SearchInput(BusinessInput):
    query: str = Field(default="", max_length=300)
    candidates: list[Candidate] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({candidate.id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("Caller candidate IDs must be unique")
        return self


class AgentSelection(BusinessInput):
    selected_candidate_ids: list[CandidateId] = Field(max_length=20)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.selected_candidate_ids)) != len(self.selected_candidate_ids):
            raise ValueError("Selected candidate IDs must be unique")
        return self


class RankedCandidate(BusinessInput):
    candidate_id: CandidateId
    normalized_name: str = Field(max_length=300)
    score: int = Field(ge=1, le=1000)


class LookupOutput(BusinessInput):
    query: str = Field(max_length=300)
    candidate_count: int = Field(ge=0, le=100)
    matches: list[RankedCandidate] = Field(max_length=20)


class SearchOutput(LookupOutput):
    selected_candidate_ids: list[CandidateId] = Field(max_length=20)
    selection_method: Literal["empty_input", "agent_assisted"]
    notice: Literal[NOTICE] = NOTICE


class ValidationInput(BusinessInput):
    search: SearchInput
    selection: AgentSelection
