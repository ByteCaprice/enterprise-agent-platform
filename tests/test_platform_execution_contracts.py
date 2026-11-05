"""Pure worker policy and new HTTP error contracts; no DB or external calls."""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from interfaces.http.platform_errors import http_exception_handler
from orchestration.platform_worker import PlatformWorker, WorkerPolicy


@pytest.mark.parametrize("changes", [
    {"lease_seconds": 0}, {"lease_seconds": 3601}, {"heartbeat_seconds": 15},
    {"heartbeat_seconds": float("nan")}, {"execution_timeout_seconds": float("inf")},
    {"execution_timeout_seconds": True}, {"execution_timeout_seconds": 86401},
    {"cancellation_grace_seconds": 61}, {"poll_seconds": -1},
])
def test_worker_timing_policy_is_finite_bounded_and_heartbeats_precede_expiry(changes):
    with pytest.raises(ValueError):
        WorkerPolicy(**changes)


@pytest.mark.parametrize("executors", [{}, {"prompt": lambda _: {}}, {"agent": None}])
def test_worker_has_no_default_or_arbitrary_published_executor(executors):
    with pytest.raises(ValueError, match="explicit"):
        PlatformWorker(object(), "worker", executors)


def test_http_error_never_discloses_exception_details_and_preserves_retry_headers():
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.request_id = "request-123"
    response = asyncio.run(http_exception_handler(request, HTTPException(
        status_code=429, detail="private-provider-detail", headers={"Retry-After": "3"},
    )))
    assert response.status_code == 429
    assert json.loads(bytes(response.body)) == {"error": {
        "code": "RATE_LIMITED", "message": "Too many requests", "request_id": "request-123",
    }}
    assert response.headers["Retry-After"] == "3"
    assert response.headers["Cache-Control"] == "no-store"


def test_http_handler_type_boundary_handles_unexpected_exception_safely():
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    response = asyncio.run(http_exception_handler(request, RuntimeError("private-credential")))
    assert response.status_code == 500 and b"private-credential" not in response.body
    assert json.loads(bytes(response.body))["error"]["request_id"] is None
