"""Offline checks for essential process roles in the example Compose file."""

from pathlib import Path

import yaml


def test_recovery_runs_as_independent_default_service():
    compose = yaml.safe_load((Path(__file__).resolve().parent.parent / "docker/platform/compose.yaml").read_text())
    recovery = compose["services"]["recovery"]
    assert recovery["command"] == ["recovery-worker", "--config-dir", "/etc/enterprise-ai"]
    assert "profiles" not in recovery
    assert "env_file" not in recovery
    assert recovery["read_only"] is True
