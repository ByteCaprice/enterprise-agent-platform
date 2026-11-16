"""Business catalog commands do not turn inspection into runtime installation."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from interfaces.cli import hub, platform
from orchestration.control_plane_auth import ControlPlanePrincipal


def options(command, bundle="remitter-search"):
    return [command, "--bundle", bundle, "--policy-key", "approved-turns", "--policy-version", "1"]


@pytest.mark.parametrize("command", ["hub-list", "hub-export"])
def test_inspection_never_initializes_deployment(command, monkeypatch, capsys):
    runtime = Mock(side_effect=AssertionError("Inspection must remain offline"))
    monkeypatch.setattr(platform, "create_runtime", runtime)
    platform.main([command] if command == "hub-list" else options(command))
    result = json.loads(capsys.readouterr().out)
    runtime.assert_not_called()
    if command == "hub-list":
        assert result[0]["key"] == "remitter-search"
    else:
        assert result["external_dependencies"] == [{"key": "approved-turns", "version": "1"}]
        flow = next(item for item in result["capabilities"] if item["spec"]["kind"] == "workflow")
        assert flow["draft"]["definition"]["definition_version"] == "workflow.code.v1"
        assert {ref["key"] for ref in result["references"]} == {"remitter-search", "remitter-search.lookup"}


@pytest.mark.parametrize("command", ["hub-export", "hub-publish", "worker"])
def test_unknown_bundle_is_rejected_before_private_configuration(command, monkeypatch, capsys):
    runtime = Mock(side_effect=AssertionError("Unknown bundles must not open deployment resources"))
    monkeypatch.setattr(platform, "create_runtime", runtime)
    if command == "worker":
        args = ["worker", "--config-dir", "unused", "--hub-bundles", "not-a-bundle"]
    else:
        args = options(command, "not-a-bundle")
        if command == "hub-publish":
            args += ["--project", "demo", "--config-dir", "unused"]
    with pytest.raises(SystemExit) as caught:
        platform.main(args)
    assert caught.value.code == 1
    runtime.assert_not_called()
    assert "not-a-bundle" not in capsys.readouterr().out


@pytest.mark.parametrize("principal", [None, ControlPlanePrincipal("service:limited", frozenset())])
def test_publication_requires_authenticated_admin_and_closes_resources(principal, monkeypatch, capsys):
    services = SimpleNamespace(close=Mock(), control_plane_auth=SimpleNamespace(authenticate=Mock(return_value=principal)))
    monkeypatch.setattr(platform, "create_runtime", Mock(return_value=services))
    publisher = Mock(side_effect=AssertionError("Unauthorized publication"))
    monkeypatch.setattr(hub, "HubPublicationService", publisher)
    monkeypatch.setenv("HUB_TEST_ADMIN_KEY", "synthetic-private-cli-token")
    with pytest.raises(SystemExit) as caught:
        platform.main(options("hub-publish") + ["--project", "demo", "--config-dir", "unused",
                                                "--api-key-env", "HUB_TEST_ADMIN_KEY"])
    assert caught.value.code == 1
    services.close.assert_called_once()
    publisher.assert_not_called()
    assert "synthetic-private-cli-token" not in capsys.readouterr().out


def test_publication_uses_env_identity_not_an_arbitrary_actor(monkeypatch, capsys):
    auth = SimpleNamespace(authenticate=Mock(return_value=ControlPlanePrincipal("reviewed-admin", frozenset({"admin"}))),
                           allowed_resources=Mock(return_value={"*"}))
    services = SimpleNamespace(close=Mock(), control_plane_auth=auth, store=SimpleNamespace(capabilities=object()))
    monkeypatch.setattr(platform, "create_runtime", Mock(return_value=services))
    publish = Mock(return_value={"atomic": False, "capabilities": []})
    monkeypatch.setattr(hub, "HubPublicationService", lambda *args: SimpleNamespace(publish=publish))
    monkeypatch.setenv("HUB_TEST_ADMIN_KEY", "synthetic-private-cli-token")
    platform.main(options("hub-publish") + ["--project", "demo", "--config-dir", "unused",
                                            "--api-key-env", "HUB_TEST_ADMIN_KEY"])
    auth.authenticate.assert_called_once_with("synthetic-private-cli-token")
    assert {call.args[2] for call in auth.allowed_resources.call_args_list} == {"capability:write", "capability:publish"}
    assert publish.call_args.args[0] == "demo" and publish.call_args.args[2] == "reviewed-admin"
    services.close.assert_called_once()
    output = capsys.readouterr().out
    assert json.loads(output)["atomic"] is False and "synthetic-private-cli-token" not in output


def test_worker_forwards_only_explicit_bundle_selection(monkeypatch, capsys):
    services = SimpleNamespace(close=Mock(), store=object())
    monkeypatch.setattr(platform, "create_runtime", Mock(return_value=services))
    calls = []

    async def worker(store, **kwargs):
        assert store is services.store
        calls.append(kwargs)
        return False

    monkeypatch.setattr(platform, "run_platform_worker", worker)
    platform.main(["worker", "--config-dir", "unused", "--once", "--executors", "workflow,agent",
                   "--workflow-agents", "--hub-bundles", "remitter-search"])
    assert calls[0]["hub_bundles"] == ("remitter-search",) and calls[0]["workflow_agents"] is True
    assert json.loads(capsys.readouterr().out)["claimed"] is False
    services.close.assert_called_once()
