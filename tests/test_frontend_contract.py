"""Small offline checks for frontend contracts; no runtime environment or UI stack."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from domain import ApiKeyConfig
from domain.http_ingress import HTTPIngressPolicy
from infra.platform_migrations import PLATFORM_SCHEMA_REVISION
from infra.rate_limiter import RateLimiter
from interfaces import runtime
from interfaces.cli import platform as cli
from interfaces.http import platform_server
from interfaces.http.api_contract import export_openapi, include_api_routes
from interfaces.http.platform_auth import platform_admin
from interfaces.http.platform_errors import http_exception_handler
from interfaces.http.routes.session import router as session_router
from orchestration.control_plane_auth import ControlPlanePrincipal


def test_cli_exports_typed_authenticated_contract_without_runtime_initialization(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Schema export must not initialize a runtime environment")

    for name in ("load_settings", "build_engine", "create_runtime"):
        monkeypatch.setattr(cli, name, forbidden)
    cli.main(["export-openapi"])
    schema = json.loads(capsys.readouterr().out)
    paths = schema["paths"]
    assert all(path.startswith("/v2/") or path == "/v2/projects" for path in paths)
    ids = []
    for item in paths.values():
        for operation in item.values():
            ids.append(operation["operationId"])
            assert operation["security"] == [{"PlatformKey": []}, {"BoundOIDC": []}]
            assert operation["responses"]["422"]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/APIErrorResponse",
            }
            assert not {"x-api-key", "authorization"} & {
                param["name"].lower() for param in operation.get("parameters", [])
            }
    assert len(ids) == len(set(ids))
    assert paths["/v2/session"]["get"]["operationId"] == "get_v2_session"
    assert paths["/v2/session"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SessionMetadata",
    }
    capabilities = paths["/v2/projects/{project_key}/capabilities"]["get"]
    assert capabilities["responses"]["200"]["content"]["application/json"]["schema"]["items"] == {
        "$ref": "#/components/schemas/CapabilityRecord",
    }
    tasks = paths["/v2/projects/{project_key}/tasks"]["post"]
    assert {"200", "202"} <= tasks["responses"].keys()
    assert any(param["name"] == "idempotency-key" and param["required"] for param in tasks["parameters"])
    assert {"200", "202"} <= paths["/v2/projects/{project}/tasks/{task_id}/recover"]["post"]["responses"].keys()
    assert schema == export_openapi()


def test_session_api_is_independent_of_console_and_still_requires_one_live_credential():
    principal = ControlPlanePrincipal("reader", frozenset(), project_key="demo", identity_id="identity")
    calls = []

    def authenticate(token):
        calls.append(token)
        return principal if token == "synthetic-key" else None

    app = FastAPI()
    app.include_router(session_router)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.state.control_plane_auth = SimpleNamespace(authenticate=authenticate,
        authenticate_bearer=lambda token: principal if token == "synthetic-jwt" else None)
    app.state.rate_limiter = RateLimiter()
    with TestClient(app) as client:
        assert client.get("/v2/session").status_code == 401
        for headers in ({"x-api-key": "synthetic-key"}, {"Authorization": "Bearer synthetic-jwt"}):
            response = client.get("/v2/session", headers=headers)
            assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
            assert response.json() == {"name": "reader", "is_admin": False, "project_key": "demo", "identity_id": "identity"}
        assert client.get("/v2/session", headers={"x-api-key": "synthetic-key",
                                                  "Authorization": "Bearer synthetic-jwt"}).status_code == 401
        assert client.get("/v2/session", headers={"Authorization": "Basic synthetic-jwt"}).status_code == 401
        assert client.get("/console/").status_code == 404
    assert "synthetic-key" in calls


def test_runtime_schema_matches_offline_contract_and_reference_console_is_opt_in(monkeypatch):
    key = ApiKeyConfig(name="admin", api_key="sha256:" + "a" * 64, scopes=["admin"])

    @contextmanager
    def connect():
        def scalar(query):
            assert str(query) in {"SELECT current_schema()", "SELECT version_num FROM alembic_version"}
            return "isolated" if "current_schema" in str(query) else PLATFORM_SCHEMA_REVISION

        yield SimpleNamespace(scalar=scalar)

    store = SimpleNamespace(engine=SimpleNamespace(connect=connect),
        api_key_configs=SimpleNamespace(list=lambda: [key]), governance=object(), oidc=None, close=Mock())
    monkeypatch.setattr(runtime, "load_settings", lambda _: {"database": {"schema": "isolated"}})
    monkeypatch.setattr(runtime, "validate_deployment", lambda *_: SimpleNamespace(http_ingress=HTTPIngressPolicy()))
    monkeypatch.setattr(runtime, "build_engine", lambda _: store.engine)
    monkeypatch.setattr(runtime, "PlatformStore", lambda _: store)
    monkeypatch.setattr(runtime, "check_validation_backend", lambda: None)
    with TestClient(platform_server.create_platform_app("unused")) as client:
        assert client.get("/").status_code == client.get("/console/").status_code == 404
        assert client.get("/v2/session").status_code == 401
        schema = client.get("/openapi.json").json()
        business_paths = {path: item for path, item in schema["paths"].items() if path.startswith("/v2")}
        assert business_paths == export_openapi()["paths"]
        assert schema["components"] == export_openapi()["components"]
    with TestClient(platform_server.create_platform_app("unused", include_reference_console=True)) as client:
        response = client.get("/console/")
        assert response.status_code == 200 and "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert client.get("/console/app.js").status_code == 200
        assert client.get("/v2/session").status_code == 401
    assert store.close.call_count == 2


def test_service_key_response_projection_exposes_token_only_at_issuance():
    metadata = {"id": "key", "project_key": "demo", "identity_id": "identity", "name": "reader", "revoked": False,
                "expires_at": None, "created_by": "admin", "create_time": datetime.now(UTC)}
    raw = {**metadata, "digest": "synthetic-hidden-digest", "token": "synthetic-hidden-token", "internal_column": "private"}
    governance = SimpleNamespace(issue_key=lambda *_: raw, list_keys=lambda *_, **kwargs: [raw], revoke_key=lambda *_: raw)
    app = FastAPI()
    include_api_routes(app)
    app.state.store = SimpleNamespace(governance=governance)
    app.dependency_overrides[platform_admin] = lambda: "admin"
    path = "/v2/projects/demo/service-identities/identity/keys"
    with TestClient(app) as client:
        response = client.post(path, json={"name": "reader"})
        assert response.status_code == 201 and response.headers["Cache-Control"] == "no-store"
        assert response.json()["token"].startswith("eai_")
        assert set(response.json()) == {*metadata, "token"}
        assert set(client.get(path).json()[0]) == set(metadata)
        assert set(client.post(path + "/key/revoke").json()) == set(metadata)
        assert "digest" not in client.get("/openapi.json").json()["components"]["schemas"]["ServiceKeyResponse"]["properties"]


def test_reference_console_option_is_rejected_outside_serve_before_runtime_initialization(monkeypatch):
    initialize = Mock(side_effect=AssertionError("Must not initialize runtime"))
    monkeypatch.setattr(cli, "create_runtime", initialize)
    with pytest.raises(SystemExit) as error:
        cli.main(["export-openapi", "--reference-console"])
    assert error.value.code == 2
    initialize.assert_not_called()
