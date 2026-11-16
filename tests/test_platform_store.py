"""The new platform composition persists only its own capability surface."""

from __future__ import annotations

import hashlib

from alembic import command
from conftest import fresh_database_url
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from test_platform_migrations import TABLES, config_for

from domain import ApiKeyConfig
from infra.platform_store import PlatformStore
from interfaces.http.platform_server import create_platform_app

ADMIN_TOKEN = "synthetic-platform-store-admin"
ADMIN = {"x-api-key": ADMIN_TOKEN}
PROJECT = "/v2/projects/platform"


def test_platform_store_is_independent_and_serves_prompt_and_task(pg_url, monkeypatch):
    from interfaces import runtime

    url = fresh_database_url(pg_url)
    command.upgrade(config_for(url), "head")
    engine = create_engine(url)
    store = PlatformStore(engine)
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    assert not any(hasattr(store, name) for name in (
        "runs", "stages", "queue", "conversations", "tools", "skills",
        "runtime_configs_old",
    ))
    store.api_key_configs.save(ApiKeyConfig(
        name="platform-test-admin", api_key="sha256:" + hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest(),
        scopes=["admin"],
    ))
    monkeypatch.setattr(runtime, "load_settings", lambda _path: {"database": {"schema": schema}})
    monkeypatch.setattr(runtime, "build_engine", lambda _settings: engine)
    statements: list[str] = []
    def record_sql(_conn, _cursor, statement, *_args):
        statements.append(statement.lower())
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        assert set(inspect(engine).get_table_names()) == TABLES | {"alembic_version"}
        with TestClient(create_platform_app("unused")) as client:
            client.app.state.rate_limiter.settings["enabled"] = False
            # Prompt publication and real render use the independent capability repository.
            assert client.post(PROJECT + "/capabilities", headers=ADMIN, json={
                "key": "welcome", "kind": "prompt", "name": "Welcome",
            }).status_code == 201
            assert client.post(PROJECT + "/capabilities/welcome/versions", headers=ADMIN, json={
                "version": "1", "definition": {"template": "Hello ${name}"},
                "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
            }).status_code == 201
            assert client.post(PROJECT + "/capabilities/welcome/versions/1/publish", headers=ADMIN).status_code == 200
            rendered = client.post(PROJECT + "/prompts/welcome/versions/1/render", headers=ADMIN,
                                   json={"variables": {"name": "Synthetic"}})
            assert rendered.status_code == 200 and rendered.json()["text"] == "Hello Synthetic"

            # Task admission is durable and does not invoke a worker/execution path.
            assert client.post(PROJECT + "/capabilities", headers=ADMIN, json={
                "key": "review", "kind": "workflow", "name": "Review",
            }).status_code == 201
            assert client.post(PROJECT + "/capabilities/review/versions", headers=ADMIN, json={
                "version": "1", "definition": {"nodes": [
                    {"key": "check", "kind": "rule", "rule": {"operation": "project"}},
                ]},
                "input_schema": {"type": "object"},
            }).status_code == 201
            assert client.post(PROJECT + "/capabilities/review/versions/1/publish", headers=ADMIN).status_code == 200
            submitted = client.post(PROJECT + "/tasks", headers={**ADMIN, "Idempotency-Key": "synthetic-1"}, json={
                "capability_key": "review", "version": "1", "input": {"note": "Synthetic"},
            })
            assert submitted.status_code == 202
            assert submitted.json()["status"] == "queued"
            assert client.get(submitted.headers["Location"], headers=ADMIN).json()["id"] == submitted.json()["id"]
        assert "ai_agent_run" not in inspect(engine).get_table_names()
        assert any("ai_task" in statement for statement in statements)
        assert any("ai_capability" in statement for statement in statements)
        assert not any(any(old in statement for old in (
            "ai_agent_run", "ai_agent_stage", "ai_conversation", "ai_callback", "ai_tool_config", "ai_skill_config",
        )) for statement in statements)
        assert store.tasks.check_ready() is None
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM ai_task")) == 1
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
        store.close()
