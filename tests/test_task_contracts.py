"""Pure task admission contracts; no database or model calls."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from domain.task import IdempotencyKey, TaskError, TaskSubmit, canonical_json


def test_request_fingerprint_is_order_independent_but_includes_target_and_input():
    first = TaskSubmit(capability_key="review", version="1", input={"b": 2, "a": {"d": 4, "c": 3}})
    same = TaskSubmit(capability_key="review", version="1", input={"a": {"c": 3, "d": 4}, "b": 2})
    assert first.fingerprint() == same.fingerprint()
    for changes in ({"version": "2"}, {"capability_key": "another"}, {"input": {"b": 3}}):
        assert TaskSubmit(**(first.model_dump() | changes)).fingerprint() != first.fingerprint()


@pytest.mark.parametrize("value", [
    {"secret": float("nan")}, {"secret": float("inf")}, {"secret": "bad\x00text"},
    {"bad\x00key": "value"}, {"secret": "\ud800"}, {"secret": "x" * 1_000_001},
])
def test_unsafe_input_is_rejected_before_persistence(value):
    with pytest.raises((TaskError, ValidationError)):
        TaskSubmit(capability_key="review", version="1", input=value)


def test_deep_json_and_non_json_values_are_rejected():
    nested = {}
    for _ in range(66):
        nested = {"nested": nested}
    for value in (nested, {1: "non-string key"}, {"object": object()}, {"tuple": (1, 2)}):
        with pytest.raises(TaskError):
            canonical_json(value)


@pytest.mark.parametrize("key", ["", "contains space", "a\n", "a" * 129, "中文", " leading", "trailing "])
def test_idempotency_key_is_a_bounded_ascii_identifier(key):
    with pytest.raises(ValidationError):
        TypeAdapter(IdempotencyKey).validate_python(key)


@pytest.mark.parametrize("extra", [{"project_key": "other"}, {"submitted_by": "admin"}, {"status": "succeeded"},
                                   {"callback_url": "https://external.test"}, {"worker_id": "spoof"}])
def test_client_cannot_inject_identity_state_or_unimplemented_delivery(extra):
    with pytest.raises(ValidationError):
        TaskSubmit(capability_key="review", version="1", **extra)


def test_version_is_explicit_and_artifact_default_is_not_a_request_target():
    with pytest.raises(ValidationError):
        TaskSubmit(capability_key="review")
