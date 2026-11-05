"""Offline production startup gate. Does not fetch policy references or delete data."""

import json
from pathlib import Path

from domain.platform_runtime import DeploymentPolicy, PlatformRuntimeSettings


def validate_deployment(config, config_dir):
    settings = PlatformRuntimeSettings.model_validate(config.get("platform_runtime", {}))
    if settings.mode == "production":
        if not settings.policy_file:
            raise ValueError("Production requires an explicitly reviewed deployment policy")
        path = Path(settings.policy_file)
        if not path.is_absolute():
            path = Path(config_dir) / path
        if path.stat().st_size > 16384:
            raise ValueError("Deployment policy is too large")
        DeploymentPolicy.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return settings
