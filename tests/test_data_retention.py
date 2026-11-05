"""Offline contract smokes for the read-only retention inventory surface."""

from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from domain.data_retention import RetentionInventory
from interfaces.http.routes.data_retention import retention_inventory, router


def test_retention_inventory_is_frozen_and_explicitly_non_destructive() -> None:
    report = RetentionInventory(
        project_key="demo",
        before=datetime(2025, 1, 1, tzinfo=UTC),
        read_time=datetime(2025, 1, 2, tzinfo=UTC),
        tasks_created_before_count=3,
        tasks_created_before_nonterminal_count=1,
        gateway_requests_created_before_count=2,
        memory_revisions_expired_as_of_read_time_and_created_before_count=4,
        budget_charges_temporal_scope="all_project_no_timestamp",
        budget_charges_all_project_count=5,
        budget_charges_all_project_unknown_count=2,
        callback_deliveries_pending_count=1,
        callback_deliveries_leased_count=2,
        callback_deliveries_dead_count=3,
    )
    assert report.automatic_deletion_supported is False
    assert report.requires_retention_policy and report.requires_reference_analysis
    assert report.budget_charges_all_project_count == 5
    assert report.budget_charges_all_project_unknown_count == 2
    assert report.callback_deliveries_pending_count == 1
    assert report.callback_deliveries_leased_count == 2
    assert report.callback_deliveries_dead_count == 3
    with pytest.raises(ValidationError):
        report.project_key = "other"
    for update in (
        {"automatic_deletion_supported": True},
        {"tasks_created_before_count": -1},
        {"unrecognized": 1},
        {"budget_charges_temporal_scope": "unknown"},
    ):
        with pytest.raises(ValidationError):
            RetentionInventory.model_validate({**report.model_dump(), **update})


def test_endpoint_requires_before_and_is_admin_protected() -> None:
    route = next(route for route in router.routes if isinstance(route, APIRoute) and route.path.endswith("retention-inventory"))
    assert route.endpoint is retention_inventory
    assert "before" in route.endpoint.__annotations__
    assert route.methods == {"GET"}
