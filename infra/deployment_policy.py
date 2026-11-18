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
        database = config.get("database", {})
        if database.get("sslmode") != "verify-full":
            raise ValueError("Production requires DATABASE__SSLMODE=verify-full")
        host = database.get("host")
        # libpq ignores sslmode on Unix sockets (including abstract sockets).
        # A comma-separated host list can also contain a socket fallback.
        if not isinstance(host, str) or not host or any(c.isspace() or c in "/@,\x00" for c in host):
            raise ValueError("Production requires a single explicit TCP DATABASE__HOST")
    return settings
