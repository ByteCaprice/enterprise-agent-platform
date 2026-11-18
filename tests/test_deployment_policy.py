"""Production transport policy checks using synthetic configuration and no database."""

import json
from unittest.mock import Mock

import pytest

from infra import database
from infra.deployment_policy import validate_deployment
from interfaces import runtime
from interfaces.cli import platform as cli
from scripts import backup_platform


@pytest.fixture
def production_config(tmp_path):
    policy = {key: "reviewed" for key in (
        "owner", "identity_policy_ref", "financial_policy_ref", "retention_policy_ref",
        "backup_restore_ref", "incident_runbook_ref", "edge_security_ref",
    )}
    (tmp_path / "policy.json").write_text(json.dumps({**policy, "shared_edge_limits_confirmed": True}))
    return {
        "platform_runtime": {"mode": "production", "policy_file": "policy.json"},
        "database": {"host": "db.example.test", "schema": "enterprise_ai_test", "sslmode": "verify-full"},
    }


@pytest.mark.parametrize("sslmode", [None, "disable", "allow", "prefer", "require", "verify-ca"])
def test_production_rejects_missing_or_weaker_tls(production_config, tmp_path, sslmode):
    config = production_config
    if sslmode is None:
        del config["database"]["sslmode"]
    else:
        config["database"]["sslmode"] = sslmode
    with pytest.raises(ValueError, match="DATABASE__SSLMODE=verify-full"):
        validate_deployment(config, tmp_path)
    config["platform_runtime"]["mode"] = "trial"
    assert validate_deployment(config, tmp_path).mode == "trial"


@pytest.mark.parametrize("host", [None, "", "/var/run/postgresql", "@abstract", "db.example.test,", " ", "a,b"])
def test_production_rejects_missing_or_non_tcp_host(production_config, tmp_path, host):
    production_config["database"]["host"] = host
    with pytest.raises(ValueError, match="TCP DATABASE__HOST"):
        validate_deployment(production_config, tmp_path)


def test_verify_full_connection_uses_tls_and_trusted_root(production_config, tmp_path, monkeypatch):
    create_engine = Mock()
    monkeypatch.setattr(database, "create_engine", create_engine)
    config = production_config["database"]
    config["host"] = "::1"
    config["sslrootcert"] = "/etc/database/root.crt"
    assert validate_deployment(production_config, tmp_path).mode == "production"
    database.build_engine(config)
    url = create_engine.call_args.args[0]
    assert url.host == "::1"
    assert dict(url.query) == {
        "sslmode": "verify-full", "gssencmode": "disable", "sslrootcert": "/etc/database/root.crt",
    }


def test_production_entry_points_reject_weak_transport_before_engine(production_config, tmp_path, monkeypatch, capsys):
    production_config["database"]["sslmode"] = "prefer"
    for module in (runtime, cli, backup_platform):
        monkeypatch.setattr(module, "load_settings", Mock(return_value=production_config))
        monkeypatch.setattr(module, "build_engine", Mock(side_effect=AssertionError("must fail before engine creation")))
    with pytest.raises(ValueError, match="verify-full"):
        runtime.create_runtime(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["migrate", "--config-dir", str(tmp_path)])
    assert exc.value.code == 1
    assert "Platform upgrade failed (ValueError)" in capsys.readouterr().out
    with pytest.raises(ValueError, match="verify-full"):
        backup_platform.backup(tmp_path)
    for module in (runtime, cli, backup_platform):
        module.build_engine.assert_not_called()
