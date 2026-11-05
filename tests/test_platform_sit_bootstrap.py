"""Bootstrap only a new named schema; never copy or mutate the source project."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import database_settings
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from test_platform_migrations import TABLES

from infra.platform_migrations import PLATFORM_SCHEMA_REVISION
from infra.platform_store import PlatformStore
from interfaces.http.platform_server import create_platform_app
from scripts import bootstrap_platform_sit as bootstrap


@pytest.mark.parametrize("schema", ["public", "unapproved_schema", "enterprise_ai,public", 'enterprise_ai"', "EnterpriseAI"])
def test_bootstrap_refuses_unapproved_or_injected_schema_names(schema):
    with pytest.raises(ValueError, match="schema"):
        bootstrap.validate_schema(schema)


def test_private_config_creation_does_not_overwrite_existing_operator_file(tmp_path):
    path = tmp_path / ".env"
    bootstrap._private_file(path, "original")
    with pytest.raises(FileExistsError):
        bootstrap._private_file(path, "replacement")
    assert path.read_text() == "original"
    assert path.stat().st_mode & 0o777 == 0o600


def test_bootstrap_creates_isolated_schema_with_digest_admin_and_no_old_agents(monkeypatch, tmp_path, pg_url):
    database = database_settings()
    monkeypatch.setattr(bootstrap, "read_test_database", lambda _path, _environment: database)
    schema = f"enterprise_ai_test_{uuid4().hex}"
    # Keep the suffix within the explicit schema naming contract.
    schema = schema[:54]
    base = URL.create("postgresql+psycopg2", host=database["host"], port=database["port"],
                      database=database["name"], username=database["user"], password=database["password"])
    admin = create_engine(base)
    config_dir = tmp_path / "new-platform"
    try:
        preflight = bootstrap.bootstrap(Path("not-used"), schema, config_dir)
        assert preflight["applied"] is False
        assert not config_dir.exists()
        result = bootstrap.bootstrap(Path("not-used"), schema, config_dir, apply=True)
        assert result["applied"] is True
        assert result["revision"] == PLATFORM_SCHEMA_REVISION
        assert result["tables"] == len(TABLES) + 1
        token = (config_dir / ".env.local").read_text().strip().split("=", 1)[1]
        store = PlatformStore(create_engine(base.update_query_dict({"options": f"-csearch_path={schema},pg_catalog"})))
        try:
            keys = store.api_key_configs.list()
            assert len(keys) == 1
            assert keys[0].api_key == "sha256:" + hashlib.sha256(token.encode()).hexdigest()
            assert set(inspect(store.engine).get_table_names()) == TABLES | {"alembic_version"}
        finally:
            store.close()
        with TestClient(create_platform_app(config_dir)) as client:
            assert client.get("/readyz").status_code == 200
            assert client.get("/v2/projects/platform/capabilities").status_code == 401
            assert client.get("/v2/projects/platform/capabilities", headers={"x-api-key": token}).json() == []
            for path in ("/v1/runs", "/agent/v1/run/submit", "/mgmt/api/v1/runtime/runs/query"):
                assert client.post(path).status_code == 404
        with pytest.raises(ValueError, match="already exists"):
            bootstrap.bootstrap(Path("not-used"), schema, config_dir, apply=True)
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
