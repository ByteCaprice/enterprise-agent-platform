"""Offline smoke checks for accepted gateway request tracing."""

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domain.gateway import GatewayAdmission, GatewayRequestContext, GatewayRequestRecord
from domain.task import TaskRecord, TaskStatus, TaskSubmit
from infra.store.model.ai_gateway_request import AiGatewayRequest
from interfaces.http.platform_auth import authenticated_principal
from interfaces.http.routes import tasks
from orchestration.gateway_service import GatewayService

NOW = datetime.now(UTC)
TASK = TaskRecord(id="task-1", project_key="payments", capability_key="review", capability_kind="agent",
                  version="1", content_hash="hash", status=TaskStatus.QUEUED, submitted_by="service:identity",
                  create_time=NOW, finish_time=None)
SPEC = TaskSubmit(capability_key="review", version="1")


def test_admission_atomic_and_reuse(monkeypatch):
    committed = []

    @contextmanager
    def uow():
        pending = []
        yield pending
        committed.extend(pending)

    def create(command, *, conn):
        assert conn and conn[0] == "task"
        if command.correlation_id == "fail":
            raise RuntimeError("evidence failed")
        conn.append(command)
        return GatewayRequestRecord(**command.model_dump(), create_time=NOW)

    store = SimpleNamespace(unit_of_work=uow, gateway_requests=SimpleNamespace(create=create))
    service = GatewayService(store, None)

    def submit(*args, conn, **kwargs):
        conn.append("task")
        return TASK, not committed

    monkeypatch.setattr(service.tasks, "submit", submit)
    with pytest.raises(RuntimeError, match="evidence failed"):
        service.submit(None, "payments", SPEC, "key", context=GatewayRequestContext(correlation_id="fail"))
    assert committed == []
    first = service.submit(None, "payments", SPEC, "key", context=GatewayRequestContext(correlation_id="one"))
    second = service.submit(None, "payments", SPEC, "key", context=GatewayRequestContext(correlation_id="two"),
                            operation="execute", delivery_mode="sync")
    assert first.task == second.task == TASK
    assert first.request.id != second.request.id
    assert (first.request.task_created, second.request.task_created) == (True, False)
    assert (first.request.correlation_id, second.request.correlation_id) == ("one", "two")
    assert (first.request.delivery_mode, second.request.delivery_mode) == ("async", "sync")
    assert len(committed) == 4
    def recover(*args, conn):
        conn.append("task")
        return TASK, False

    monkeypatch.setattr(service.tasks, "recover", recover)
    recovered = service.recover(None, "payments", "source", "key", context=GatewayRequestContext())
    assert recovered.request.operation == "recover"
    assert recovered.request.task_created is False


def test_read_scope_forwarded_for_owner_and_admin():
    repository = SimpleNamespace(get=Mock(), list=Mock())
    auth = SimpleNamespace(allowed_resources=Mock(return_value={"review"}))
    service = GatewayService(SimpleNamespace(gateway_requests=repository), auth)
    principal = SimpleNamespace(identity_id="identity", name="user", has_scope=lambda scope: False)
    service.get(principal, "payments", "a" * 32)
    service.list(principal, "payments", "task-1", limit=2, cursor="cursor")
    repository.get.assert_called_once_with("payments", "a" * 32, allowed_keys={"review"}, owner="service:identity")
    repository.list.assert_called_once_with("payments", "task-1", allowed_keys={"review"},
                                            owner="service:identity", limit=2, cursor="cursor")
    assert auth.allowed_resources.call_args_list == [((principal, "payments", "task:read"),)] * 2
    principal.has_scope = lambda scope: scope == "admin"
    service.get(principal, "payments", "b" * 32)
    assert repository.get.call_args.kwargs["owner"] is None


def test_http_generated_identity_and_sync_wait(monkeypatch):
    app = FastAPI()
    app.include_router(tasks.router)
    app.state.store = object()
    app.state.control_plane_auth = object()
    app.dependency_overrides[authenticated_principal] = lambda: object()

    class FakeGateway:
        def __init__(self, store, auth):
            pass

        def submit(self, principal, project, spec, key, *, context, operation, delivery_mode):
            record = GatewayRequestRecord(id=context.request_id, correlation_id=context.correlation_id,
                project_key=project, task_id=TASK.id, submitted_by=TASK.submitted_by, operation=operation,
                delivery_mode=delivery_mode, task_created=True, create_time=NOW)
            return GatewayAdmission(task=TASK, request=record)

    monkeypatch.setattr(tasks, "GatewayService", FakeGateway)
    monkeypatch.setattr(tasks, "_service", lambda request: SimpleNamespace(_access=lambda *args: None))
    client = TestClient(app)
    payload = {"task": {"capability_key": "review", "version": "1", "input": {}},
               "mode": "sync", "wait_seconds": 0.001}
    headers = {"Idempotency-Key": "key", "X-Request-ID": "f" * 32, "X-Correlation-ID": "trace:1"}
    response = client.post("/v2/projects/payments/tasks/execute", json=payload, headers=headers)
    assert response.status_code == 202
    body = response.json()
    assert body["wait_timed_out"] is True
    assert body["request"]["id"] == response.headers["X-Request-ID"] != headers["X-Request-ID"]
    assert body["request"]["correlation_id"] == response.headers["X-Correlation-ID"] == "trace:1"
    assert body["request"]["delivery_mode"] == "sync"
    headers["X-Correlation-ID"] = "bad token"
    assert client.post("/v2/projects/payments/tasks/execute", json=payload, headers=headers).status_code == 422


def test_migration_matches_model_metadata():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    sql = (Path(__file__).resolve().parents[1] / "infra/platform_migrations/versions/schema.sql").read_text()
    sql = " ".join(sql.split())
    table = AiGatewayRequest.__table__
    assert " ".join((str(CreateTable(table).compile(dialect=postgresql.dialect())).strip() + ";").split()) in sql
    for index in table.indexes:
        assert " ".join((str(CreateIndex(index).compile(dialect=postgresql.dialect())).strip() + ";").split()) in sql


def test_schema_planning_accepts_current_revision_and_rejects_unknown_revision():
    from infra.platform_migrations import PLATFORM_SCHEMA_REVISION
    from infra.platform_migrations.runner import upgrade_platform

    class Connection:
        revision = ""

        def scalar(self, statement):
            query = str(statement)
            if query == "SELECT current_schema()":
                return "prototype_schema"
            assert query == "SHOW search_path"
            return "prototype_schema, pg_catalog"

        def scalars(self, statement):
            assert str(statement) == "SELECT version_num FROM alembic_version"
            return SimpleNamespace(all=lambda: [self.revision])

        def execute(self, statement):
            assert "pg_advisory_xact_lock" in str(statement)

    connection = Connection()

    @contextmanager
    def begin():
        yield connection

    engine = SimpleNamespace(begin=begin)
    connection.revision = PLATFORM_SCHEMA_REVISION
    plan = upgrade_platform(engine, "prototype_schema")
    assert plan["from_revision"] == plan["target_revision"] == PLATFORM_SCHEMA_REVISION
    assert not plan["applied"] and not plan["changed"]
    connection.revision = "unrecognized_revision"
    with pytest.raises(ValueError, match="Unknown schema revision"):
        upgrade_platform(engine, "prototype_schema")
