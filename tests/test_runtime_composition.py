"""Offline lifecycle and framework-boundary checks; all persistence is synthetic."""

import json
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from domain.platform_runtime import PlatformRuntimeSettings
from infra.platform_migrations import PLATFORM_SCHEMA_REVISION
from interfaces import runtime
from interfaces.cli import platform as cli
from interfaces.platform_settings import SettingsValidationError


@pytest.fixture
def deployment(monkeypatch):
    queries = []

    @contextmanager
    def connect():
        def scalar(query):
            queries.append(str(query))
            return "isolated" if str(query) == "SELECT current_schema()" else PLATFORM_SCHEMA_REVISION

        yield SimpleNamespace(scalar=scalar)

    store = SimpleNamespace(engine=SimpleNamespace(connect=connect, dispose=Mock()), close=Mock(),
        api_key_configs=SimpleNamespace(list=Mock(return_value=[SimpleNamespace(api_key="sha256:" + "a" * 64)])),
        oidc=Mock())
    for name in ("capabilities", "governance", "tasks", "models", "tool_bindings", "agents", "task_events",
                 "gateway_requests", "operations", "worker_health"):
        setattr(store, name, Mock())
    bootstrap = {"database": {"schema": "isolated"}, "server": {"host": "127.0.0.1", "port": 1234}}
    settings = PlatformRuntimeSettings()
    monkeypatch.setattr(runtime, "load_settings", Mock(return_value=bootstrap))
    monkeypatch.setattr(runtime, "validate_deployment", Mock(return_value=settings))
    monkeypatch.setattr(runtime, "build_engine", Mock(return_value=store.engine))
    monkeypatch.setattr(runtime, "PlatformStore", Mock(return_value=store))
    monkeypatch.setattr(runtime, "check_validation_backend", Mock())
    return SimpleNamespace(store=store, bootstrap=bootstrap, settings=settings, queries=queries)


def test_headless_composition_validates_without_enabling_or_mutating_configuration(deployment):
    with runtime.create_runtime("unused") as services:
        assert services.bootstrap is deployment.bootstrap
        assert services.runtime_settings is deployment.settings
        assert services.control_plane_auth.key_repository is deployment.store.api_key_configs
        assert services.control_plane_auth.governance is deployment.store.governance
        assert services.control_plane_auth.oidc is deployment.store.oidc
        assert deployment.queries == ["SELECT current_schema()", "SELECT version_num FROM alembic_version"]
        assert deployment.bootstrap["database"] == {"schema": "isolated"}
        runtime.build_engine.assert_called_once_with({"schema": "isolated", "statement_timeout_ms": 15000,
                                                     "lock_timeout_ms": 5000, "pool_timeout_seconds": 10})
        runtime.check_validation_backend.assert_called_once_with()
        deployment.store.tasks.assert_not_called()
        deployment.store.operations.assert_not_called()
        deployment.store.close.assert_not_called()
    services.close()
    deployment.store.close.assert_called_once_with()


def test_explicit_database_timeouts_are_preserved(deployment):
    deployment.bootstrap["database"].update(statement_timeout_ms=123, lock_timeout_ms=456, pool_timeout_seconds=7)
    runtime.create_runtime("unused").close()
    assert runtime.build_engine.call_args.args[0] == deployment.bootstrap["database"]


@pytest.mark.parametrize("schema", [None, "", "public", "pg_catalog", "information_schema"])
def test_isolated_schema_required_before_engine_creation(deployment, schema):
    deployment.bootstrap["database"]["schema"] = schema
    with pytest.raises(SettingsValidationError, match="isolated"):
        runtime.create_runtime("unused")
    runtime.build_engine.assert_not_called()


def test_deployment_validation_precedes_resource_acquisition(deployment):
    runtime.validate_deployment.side_effect = ValueError("reviewed policy required")
    with pytest.raises(ValueError):
        runtime.create_runtime("unused")
    runtime.build_engine.assert_not_called()


@pytest.mark.parametrize("stage", ["backend", "schema", "revision", "keys", "auth", "interrupt"])
def test_failed_runtime_initialization_closes_store(deployment, monkeypatch, stage):
    if stage == "backend":
        runtime.check_validation_backend.side_effect = RuntimeError("missing backend")
    elif stage in {"schema", "revision"}:
        @contextmanager
        def connect():
            values = ["wrong" if stage == "schema" else "isolated",
                      "old" if stage == "revision" else PLATFORM_SCHEMA_REVISION]
            yield SimpleNamespace(scalar=lambda _: values.pop(0))

        deployment.store.engine.connect = connect
    elif stage == "keys":
        deployment.store.api_key_configs.list.side_effect = RuntimeError("query failed")
    elif stage == "auth":
        monkeypatch.setattr(runtime, "ControlPlaneAuth", Mock(side_effect=RuntimeError("auth failed")))
    else:
        runtime.check_validation_backend.side_effect = KeyboardInterrupt()
    with pytest.raises((RuntimeError, SettingsValidationError, KeyboardInterrupt)):
        runtime.create_runtime("unused")
    deployment.store.close.assert_called_once_with()


@pytest.mark.parametrize("keys", [[], ["plain-key"], ["sha256:" + "A" * 64], ["sha256:" + "a" * 63],
                                  ["sha256:" + "a" * 64, "plain-key"]])
def test_digest_backed_key_validation_is_not_bypassed(deployment, keys):
    deployment.store.api_key_configs.list.return_value = [SimpleNamespace(api_key=key) for key in keys]
    with pytest.raises(SettingsValidationError, match="digest-backed"):
        runtime.create_runtime("unused")
    deployment.store.close.assert_called_once_with()


def test_failed_store_construction_disposes_acquired_engine(deployment):
    runtime.PlatformStore.side_effect = RuntimeError("repository construction failed")
    with pytest.raises(RuntimeError):
        runtime.create_runtime("unused")
    deployment.store.engine.dispose.assert_called_once_with()


def test_readiness_checks_stay_in_headless_services(deployment):
    with runtime.create_runtime("unused") as services:
        services.check_ready()
        deployment.store.capabilities.list.assert_called_once_with("platform", limit=1)
        deployment.store.governance.get_project.assert_called_once_with("platform")
        for name in ("governance", "tasks", "models", "tool_bindings", "agents", "task_events",
                     "gateway_requests", "operations"):
            getattr(deployment.store, name).check_ready.assert_called_once_with()


def test_http_wraps_injected_services_and_closes_on_shutdown(deployment, monkeypatch):
    from fastapi.testclient import TestClient

    from interfaces.http import platform_server

    services = runtime.create_runtime("unused")
    initialize = Mock(side_effect=AssertionError("Must reuse services"))
    monkeypatch.setattr(platform_server, "create_runtime", initialize)
    app = platform_server.create_platform_app(runtime=services)
    assert app.state.runtime is services
    assert app.state.store is services.store
    assert app.state.control_plane_auth is services.control_plane_auth
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        deployment.store.tasks.check_ready.side_effect = RuntimeError("sensitive details")
        response = client.get("/readyz")
        assert response.status_code == 503 and response.json() == {"status": "not_ready"}
        assert client.get("/livez").status_code == 200
    services.close()
    deployment.store.close.assert_called_once_with()
    initialize.assert_not_called()


@pytest.mark.parametrize("stage", ["rate_limiter", "routes"])
def test_http_construction_failure_closes_services(deployment, monkeypatch, stage):
    from interfaces.http import platform_server

    services = runtime.create_runtime("unused")
    monkeypatch.setattr(platform_server, "RateLimiter" if stage == "rate_limiter" else "include_api_routes",
                        Mock(side_effect=RuntimeError("HTTP composition failed")))
    with pytest.raises(RuntimeError):
        platform_server.create_platform_app(runtime=services)
    deployment.store.close.assert_called_once_with()


@pytest.mark.parametrize("command,extra", [
    ("check-config", []), ("health", ["--project", "demo"]),
    ("check-worker", ["--worker-id", "worker"]), ("worker", ["--once"]),
    ("worker", ["--once", "--executors", "agent"]),
    ("worker", ["--once", "--workflow-agents"]),
    ("callback-worker", ["--once"]), ("recovery-worker", ["--once"]),
])
def test_cli_commands_use_services_and_close_without_http(deployment, monkeypatch, capsys, command, extra):
    services = runtime.create_runtime("unused")
    deployment.store.worker_health.summary.return_value = {"status": "ok"}
    deployment.store.worker_health.live.return_value = ["worker"]
    monkeypatch.setattr(cli, "create_runtime", Mock(return_value=services))
    calls = []

    async def worker(store, **kwargs):
        assert store is services.store
        calls.append(kwargs)
        return True

    for name in ("run_workflow_worker", "run_platform_worker", "run_callback_worker", "run_recovery_worker"):
        monkeypatch.setattr(cli, name, worker)
    cli.main([command, "--config-dir", "unused", *extra])
    deployment.store.close.assert_called_once_with()
    assert json.loads(capsys.readouterr().out)
    if command == "worker":
        assert calls[0]["mcp_catalog"] is None
        if "--executors" in extra:
            assert calls[0]["execution_kinds"] == ("agent",)
        elif "--workflow-agents" in extra:
            assert calls[0]["workflow_agents"] is True
        else:
            assert "execution_kinds" not in calls[0] and "workflow_agents" not in calls[0]


@pytest.mark.parametrize("command", ["worker", "callback-worker", "recovery-worker"])
@pytest.mark.parametrize("error", [RuntimeError("sensitive worker data"), KeyboardInterrupt()])
def test_cli_worker_failure_and_interruption_close_services(deployment, monkeypatch, capsys, command, error):
    services = runtime.create_runtime("unused")
    monkeypatch.setattr(cli, "create_runtime", lambda _: services)

    async def fail(*_, **kwargs):
        raise error

    for name in ("run_workflow_worker", "run_callback_worker", "run_recovery_worker"):
        monkeypatch.setattr(cli, name, fail)
    with pytest.raises(KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else SystemExit):
        cli.main([command, "--config-dir", "unused", "--once"])
    deployment.store.close.assert_called_once_with()
    assert "sensitive worker data" not in capsys.readouterr().out


@pytest.mark.parametrize("unavailable", [False, True])
def test_cli_unhealthy_and_failed_health_close_services(deployment, monkeypatch, capsys, unavailable):
    services = runtime.create_runtime("unused")
    monkeypatch.setattr(cli, "create_runtime", lambda _: services)
    deployment.store.worker_health.summary.return_value = {"status": "degraded"}
    if unavailable:
        deployment.store.worker_health.summary.side_effect = RuntimeError("sensitive health data")
    with pytest.raises(SystemExit) as exc:
        cli.main(["health", "--config-dir", "unused", "--project", "demo"])
    assert exc.value.code == (2 if unavailable else 1)
    deployment.store.close.assert_called_once_with()
    assert "sensitive health data" not in capsys.readouterr().out


def test_cli_serve_closes_even_if_server_never_enters_lifespan(deployment, monkeypatch):
    import uvicorn

    from interfaces.http import platform_server

    services = runtime.create_runtime("unused")
    monkeypatch.setattr(cli, "create_runtime", lambda _: services)
    wrap = Mock(return_value=object())
    monkeypatch.setattr(platform_server, "create_platform_app", wrap)
    monkeypatch.setattr(uvicorn, "run", Mock(side_effect=RuntimeError("bind failed")))
    with pytest.raises(RuntimeError):
        cli.main(["serve", "--config-dir", "unused", "--reference-console"])
    wrap.assert_called_once_with(runtime=services, include_reference_console=True)
    deployment.store.close.assert_called_once_with()


def test_cli_http_initialization_failure_is_sanitized_and_closes_services(deployment, monkeypatch, capsys):
    from interfaces.http import platform_server

    services = runtime.create_runtime("unused")
    monkeypatch.setattr(cli, "create_runtime", lambda _: services)
    monkeypatch.setattr(platform_server, "create_platform_app", Mock(side_effect=RuntimeError("sensitive settings")))
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--config-dir", "unused"])
    assert exc.value.code == 1
    assert "sensitive settings" not in capsys.readouterr().out
    deployment.store.close.assert_called_once_with()


def test_cli_check_config_output_failure_still_closes_services(deployment, monkeypatch):
    services = runtime.create_runtime("unused")
    monkeypatch.setattr(cli, "create_runtime", lambda _: services)
    monkeypatch.setattr("builtins.print", Mock(side_effect=BrokenPipeError()))
    with pytest.raises(BrokenPipeError):
        cli.main(["check-config", "--config-dir", "unused"])
    deployment.store.close.assert_called_once_with()


def test_cli_missing_worker_still_closes_services(deployment, monkeypatch, capsys):
    services = runtime.create_runtime("unused")
    monkeypatch.setattr(cli, "create_runtime", lambda _: services)
    deployment.store.worker_health.live.return_value = []
    with pytest.raises(SystemExit) as exc:
        cli.main(["check-worker", "--config-dir", "unused", "--worker-id", "missing-worker"])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "missing"
    deployment.store.close.assert_called_once_with()


def test_headless_import_and_commands_cannot_load_http_stack():
    code = '''
import importlib.abc
import sys
from types import SimpleNamespace

class NoHTTP(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if (fullname.split(".")[0] in {"fastapi", "starlette", "uvicorn"}
                or fullname.startswith("interfaces.http") or fullname == "infra.rate_limiter"):
            raise AssertionError("Headless process imported " + fullname)

sys.meta_path.insert(0, NoHTTP())
from interfaces import runtime
from interfaces.cli import platform as cli

closed = []
def initialize(_):
    return SimpleNamespace(runtime_settings=SimpleNamespace(mode="trial", worker_stale_seconds=30,
        queue_warning_seconds=60), close=lambda: closed.append(True), store=SimpleNamespace(
        worker_health=SimpleNamespace(summary=lambda *a, **kw: {"status": "ok"}, live=lambda **kw: ["worker"])))

async def worker(*args, **kwargs):
    return True

cli.create_runtime = initialize
cli.run_workflow_worker = cli.run_platform_worker = worker
cli.run_callback_worker = cli.run_recovery_worker = worker
for command, extra in [("check-config", []), ("health", ["--project", "demo"]),
    ("check-worker", ["--worker-id", "worker"]), ("worker", ["--once"]),
    ("callback-worker", ["--once"]), ("recovery-worker", ["--once"])]:
    cli.main([command, "--config-dir", "unused", *extra])
assert len(closed) == 6
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
