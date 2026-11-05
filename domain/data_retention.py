"""Read-only, observed project retention inventory; counts are not deletion eligibility."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from domain.capability import Key


class RetentionInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_key: Key
    before: AwareDatetime
    read_time: AwareDatetime
    tasks_created_before_count: int = Field(ge=0)
    tasks_created_before_nonterminal_count: int = Field(ge=0)
    gateway_requests_created_before_count: int = Field(ge=0)
    memory_revisions_expired_as_of_read_time_and_created_before_count: int = Field(ge=0)
    budget_charges_temporal_scope: Literal["all_project_no_timestamp"] = "all_project_no_timestamp"
    budget_charges_all_project_count: int = Field(ge=0)
    budget_charges_all_project_unknown_count: int = Field(ge=0)
    callback_deliveries_pending_count: int = Field(ge=0)
    callback_deliveries_leased_count: int = Field(ge=0)
    callback_deliveries_dead_count: int = Field(ge=0)
    counts_are_observed_not_safe_to_delete_or_eligible: Literal[True] = True
    automatic_deletion_supported: Literal[False] = False
    requires_retention_policy: Literal[True] = True
    requires_reference_analysis: Literal[True] = True


class RetentionInventoryError(Exception):
    """Base error for inventory request failures."""


class RetentionProjectNotFound(RetentionInventoryError):
    pass


class RetentionCutoffAfterReadTime(RetentionInventoryError):
    pass
