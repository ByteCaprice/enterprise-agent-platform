"""New platform bootstrap has no business-agent settings or defaults."""

import pytest
from pydantic import ValidationError

from interfaces.platform_settings import load_settings


def test_platform_settings_only_load_process_configuration(tmp_path, monkeypatch):
    (tmp_path / "settings.yaml").write_text(
        "database:\n  host: yaml-db\n  schema: platform\nserver:\n  port: 1234\n"
        "business_feature:\n  enabled: true\nmodel:\n  api_key: synthetic-secret\n"
    )
    (tmp_path / ".env").write_text('DATABASE__HOST="dotenv-db"\nSERVER__PORT=2345\n')
    monkeypatch.setenv("DATABASE__HOST", "env-db")
    monkeypatch.setenv("BUSINESS_FEATURE_ENABLED", "true")

    settings = load_settings(tmp_path)
    assert set(settings) == {"database", "server", "platform_runtime"}
    assert settings["database"]["host"] == "env-db"
    assert settings["database"]["schema"] == "platform"
    assert settings["server"]["port"] == 2345
    assert settings["platform_runtime"]["mode"] == "trial"


def test_platform_settings_reject_invalid_database_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE__BACKEND", "sqlite")
    with pytest.raises(ValidationError):
        load_settings(tmp_path)
