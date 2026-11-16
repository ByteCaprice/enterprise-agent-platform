"""Headless process composition and ownership of platform resources.

Construction validates an existing deployment; it never migrates the database or
starts executors. HTTP applications and CLI commands share this lifecycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text

from domain.platform_runtime import PlatformRuntimeSettings
from infra.database import build_engine
from infra.deployment_policy import validate_deployment
from infra.platform_migrations import PLATFORM_SCHEMA_REVISION
from infra.platform_store import PlatformStore
from infra.schema_validation import check_validation_backend
from interfaces.platform_settings import SettingsValidationError, load_settings
from orchestration.control_plane_auth import ControlPlaneAuth


@dataclass
class RuntimeServices:
    """Process services; the owner must close them, including on startup failure."""

    bootstrap: dict[str, Any]
    runtime_settings: PlatformRuntimeSettings
    store: PlatformStore
    control_plane_auth: ControlPlaneAuth
    schema: str
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        """Allow HTTP lifespan and its process owner to safely share cleanup."""
        if not self._closed:
            self._closed = True
            self.store.close()

    def __enter__(self) -> RuntimeServices:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def check_ready(self) -> None:
        """Check existing schema and repositories without repairing or enabling them."""
        with self.store.engine.connect() as connection:
            if connection.scalar(text("SELECT current_schema()")) != self.schema:
                raise RuntimeError("Schema isolation lost")
            if connection.scalar(text("SELECT version_num FROM alembic_version")) != PLATFORM_SCHEMA_REVISION:
                raise RuntimeError("Unsupported schema revision")
        self.store.capabilities.list("platform", limit=1)
        self.store.governance.get_project("platform")
        self.store.governance.check_ready()
        self.store.tasks.check_ready()
        self.store.models.check_ready()
        self.store.tool_bindings.check_ready()
        self.store.agents.check_ready()
        self.store.task_events.check_ready()
        self.store.gateway_requests.check_ready()
        self.store.operations.check_ready()


def create_runtime(config_dir: str | Path) -> RuntimeServices:
    """Validate explicit configuration and acquire services without any HTTP stack."""
    config_dir = Path(config_dir).resolve()
    bootstrap = load_settings(config_dir)
    runtime_settings = validate_deployment(bootstrap, config_dir)
    database = dict(bootstrap.get("database", {}))
    for name, value in {"statement_timeout_ms": 15000, "lock_timeout_ms": 5000, "pool_timeout_seconds": 10}.items():
        if database.get(name) is None:
            database[name] = value
    schema = database.get("schema")
    if not schema or schema in {"public", "pg_catalog", "information_schema"}:
        raise SettingsValidationError("New platform requires an explicit, isolated DATABASE__SCHEMA")
    engine = build_engine(database)
    try:
        store = PlatformStore(engine)
    except BaseException:
        engine.dispose()
        raise
    try:
        check_validation_backend()
        with store.engine.connect() as connection:
            actual = connection.scalar(text("SELECT current_schema()"))
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if actual != schema or revision != PLATFORM_SCHEMA_REVISION:
            raise SettingsValidationError("Database is not a supported new-platform schema")
        keys = store.api_key_configs.list()
        if not keys or any(re.fullmatch(r"sha256:[a-f0-9]{64}", key.api_key) is None for key in keys):
            raise SettingsValidationError("New platform requires digest-backed API keys")
        auth = ControlPlaneAuth(store.api_key_configs, store.governance, store.oidc)
        return RuntimeServices(bootstrap, runtime_settings, store, auth, schema)
    except BaseException:
        store.close()
        raise
