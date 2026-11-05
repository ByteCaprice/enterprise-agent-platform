"""Real platform bootstrap with isolated schema and new process configuration."""

import hashlib
import json

from alembic import command
from conftest import fresh_database_url
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_platform_migrations import config_for

from domain import ApiKeyConfig
from infra.platform_store import PlatformStore
from interfaces.http.platform_server import create_platform_app


def test_app_bootstraps_from_platform_settings(pg_url, tmp_path, monkeypatch):
    url = fresh_database_url(pg_url)
    command.upgrade(config_for(url), "head")
    store = PlatformStore(create_engine(url))
    try:
        store.api_key_configs.save(ApiKeyConfig(
            name="bootstrap-test", api_key="sha256:" + hashlib.sha256(b"synthetic-key").hexdigest(), scopes=["admin"]
        ))
        with store.engine.connect() as conn:
            schema = conn.scalar(text("SELECT current_schema()"))
    finally:
        store.close()

    parsed = make_url(url)
    settings = {
        "DATABASE__HOST": parsed.host, "DATABASE__PORT": parsed.port,
        "DATABASE__NAME": parsed.database, "DATABASE__USER": parsed.username,
        "DATABASE__PASSWORD": parsed.password, "DATABASE__SCHEMA": schema,
        "DATABASE__SSLMODE": "disable",
    }
    for key in settings:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("".join(f"{key}={json.dumps(str(value))}\n" for key, value in settings.items()))
    with TestClient(create_platform_app(tmp_path)) as client:
        assert client.get("/readyz").status_code == 200
