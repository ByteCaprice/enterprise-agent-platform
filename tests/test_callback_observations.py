"""Offline contract, query and authorization checks; never send a callback."""

import base64
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql
from starlette.exceptions import HTTPException
from test_gateway_requests import TASK

from domain.callback_delivery import CallbackDeliveryView
from domain.governance import AccessDenied
from domain.task import TaskError, TaskNotFound
from infra.store.repository.callbacks import CallbackRepository, _delivery_cursor
from interfaces.http.platform_auth import authenticated_principal
from interfaces.http.platform_errors import http_exception_handler
from interfaces.http.routes.gateway import router
from orchestration.task_service import TaskService

NOW = datetime.now(UTC)
SCOPE = {"allowed_keys": {"review"}, "owner": "service:identity"}


def delivery(id="a" * 32, **changes):
    return {"id": id, "project_key": "payments", "task_id": TASK.id, "endpoint_key": "notify",
            "event_sequence": 3, "state": "pending", "attempts": 0, "next_attempt_at": NOW,
            "http_status": None, "error_code": None, **changes}


def repository(rows, *, visible=True):
    queries = []

    class Session:
        def execute(self, statement):
            if not isinstance(statement, Select):
                assert "REPEATABLE READ, READ ONLY" in str(statement)
                return
            queries.append(statement)
            if "submitted_by" in statement.selected_columns.keys():
                return SimpleNamespace(mappings=lambda: SimpleNamespace(
                    one_or_none=lambda: TASK.model_dump() if visible else None))
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

        def scalar(self, statement):
            assert str(statement) == "SELECT transaction_timestamp()"
            return NOW

        @contextmanager
        def begin(self):
            yield

    @contextmanager
    def factory():
        yield Session()

    factory.kw = {"bind": object()}
    return CallbackRepository(factory), queries


def test_projection_is_allowlisted_scoped_and_bounded_and_cursor_is_stable():
    repo, queries = repository([delivery(), delivery("b" * 32)])
    page = repo.task_deliveries("payments", TASK.id, **SCOPE, limit=1)
    assert page.read_time == NOW and page.consistency == "page_snapshot"
    assert [row.id for row in page.items] == ["a" * 32]
    assert page.next_cursor
    parent, query = queries
    parent_sql = str(parent.compile(dialect=postgresql.dialect()))
    assert "ai_task.project_key =" in parent_sql and "ai_task.id =" in parent_sql
    assert "ai_task.submitted_by =" in parent_sql and "ai_capability.key IN" in parent_sql
    assert set(query.selected_columns.keys()) == set(CallbackDeliveryView.model_fields)
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert "ai_callback_delivery.project_key =" in sql and "ai_callback_delivery.task_id =" in sql
    assert "ORDER BY ai_callback_delivery.event_sequence, ai_callback_delivery.id" in sql
    assert query.compile().params["param_1"] == 2
    assert not {"payload", "lease_token", "lease_expires_at", "credential_ref", "url"} & set(query.selected_columns.keys())

    # A delivery may settle between pages; mutable retry timestamps and state
    # must not be part of the continuation position.
    repo, queries = repository([delivery("b" * 32, state="delivered", attempts=1, http_status=204)])
    final = repo.task_deliveries("payments", TASK.id, **SCOPE, limit=1, cursor=page.next_cursor)
    assert final.next_cursor is None and final.items[0].state == "delivered"
    sql = str(queries[-1].compile(dialect=postgresql.dialect()))
    assert "(ai_callback_delivery.event_sequence, ai_callback_delivery.id) >" in sql
    assert "next_attempt_at >" not in sql
    for changes in ({"project": "other"}, {"task_id": "other"}, {"owner": "service:other"}, {"allowed_keys": {"*"}}):
        args = {"project": "payments", "task_id": TASK.id, **SCOPE, **changes}
        with pytest.raises(TaskError, match="cursor"):
            repo.task_deliveries(**args, cursor=page.next_cursor)


@pytest.mark.parametrize("limit", [0, 201, True, 1.5, "1"])
def test_limit_validation_never_opens_database(limit):
    factory = Mock(kw={"bind": object()}, side_effect=AssertionError("Must not open database"))
    with pytest.raises(TaskError, match="limit"):
        CallbackRepository(factory).task_deliveries("payments", TASK.id, **SCOPE, limit=limit)
    factory.assert_not_called()


def test_hidden_parent_never_reads_delivery_or_accepts_cursor_as_authority():
    repo, queries = repository([], visible=False)
    with pytest.raises(TaskNotFound):
        repo.task_deliveries("payments", TASK.id, **SCOPE, cursor="invalid")
    assert len(queries) == 1
    repo, _ = repository([])
    page = repo.task_deliveries("payments", TASK.id, **SCOPE)
    assert page.items == () and page.next_cursor is None


@pytest.mark.parametrize("value", ["", "!", "a" * 2049, 42, "e30", "bnVsbA"])
def test_invalid_cursors_fail_closed(value):
    with pytest.raises(TaskError, match="cursor"):
        _delivery_cursor(value, "a" * 64)


def test_cursor_rejects_extra_fields_and_non_integer_sequence():
    for changes in ({"sequence": True}, {"sequence": 0}, {"sequence": 2**63}, {"sequence": "3"}, {"version": 2}, {"extra": "field"}):
        payload = {"version": 1, "scope": "a" * 64, "sequence": 3, "id": "a" * 32, **changes}
        value = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        with pytest.raises(TaskError, match="cursor"):
            _delivery_cursor(value, "a" * 64)


def test_service_reauthorizes_each_page_and_only_admin_bypasses_owner():
    callbacks = SimpleNamespace(task_deliveries=Mock())
    auth = SimpleNamespace(allowed_resources=Mock(return_value={"review"}))
    service = TaskService(SimpleNamespace(callbacks=callbacks), auth)
    principal = SimpleNamespace(identity_id="identity", name="caller", has_scope=lambda scope: False)
    service.deliveries(principal, "payments", TASK.id, limit=1, cursor="position")
    callbacks.task_deliveries.assert_called_once_with("payments", TASK.id, **SCOPE, limit=1, cursor="position")
    principal.has_scope = lambda scope: scope == "admin"
    service.deliveries(principal, "payments", TASK.id)
    assert callbacks.task_deliveries.call_args.kwargs["owner"] is None
    auth.allowed_resources.side_effect = AccessDenied("revoked")
    with pytest.raises(AccessDenied):
        service.deliveries(principal, "payments", TASK.id, cursor="position")
    assert callbacks.task_deliveries.call_count == 2
    assert auth.allowed_resources.call_count == 3


def test_http_metadata_contract_validation_and_safe_errors():
    repo, _ = repository([delivery()])
    principal = SimpleNamespace(identity_id="identity", name="caller", has_scope=lambda scope: False)
    auth = SimpleNamespace(allowed_resources=Mock(return_value={"review"}))
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.dependency_overrides[authenticated_principal] = lambda: principal
    app.state.store = SimpleNamespace(callbacks=repo)
    app.state.control_plane_auth = auth
    path = f"/v2/projects/payments/tasks/{TASK.id}/deliveries"
    with TestClient(app) as client:
        response = client.get(path)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        body = response.json()
        assert set(body) == {"items", "next_cursor", "read_time", "consistency"}
        assert set(body["items"][0]) == set(CallbackDeliveryView.model_fields)
        for params in ({"limit": 0}, {"limit": 201}, {"cursor": ""}, {"cursor": "a" * 2049}):
            assert client.get(path, params=params).status_code == 422
        for exception, status in ((AccessDenied("private"), 403), (TaskNotFound("private"), 404), (TaskError("private"), 422)):
            auth.allowed_resources.side_effect = exception
            response = client.get(path)
            assert response.status_code == status and "private" not in response.text
            assert response.headers["Cache-Control"] == "no-store"
