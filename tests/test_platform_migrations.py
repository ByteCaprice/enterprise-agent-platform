"""Fresh baseline, schema isolation and explicit refusal of implicit adoption."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest import fresh_database_url
from sqlalchemy import create_engine, inspect, text

from infra.platform_migrations import PLATFORM_SCHEMA_REVISION
from infra.platform_migrations.runner import upgrade_platform

TABLES = {
    "ai_capability", "ai_capability_version", "ai_api_key_config", "ai_run_log", "ai_project",
    "ai_service_identity", "ai_service_access_key", "ai_resource_grant", "ai_task", "ai_gateway_request",
    "ai_task_attempt", "ai_workflow_node", "ai_platform_model_provider", "ai_platform_model_call",
    "ai_platform_tool_binding", "ai_platform_tool_call", "ai_agent_execution", "ai_agent_step", "ai_task_event",
    "ai_project_limits", "ai_budget_cycle", "ai_budget_charge", "ai_callback_endpoint", "ai_callback_delivery",
    "ai_quality_dataset", "ai_quality_run", "ai_task_review", "ai_memory_fact", "ai_oidc_provider", "ai_oidc_binding",
    "ai_worker_health", "ai_global_concurrency", "ai_capability_concurrency", "ai_task_feedback",
}


def config_for(url=None):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "infra/platform_migrations"))
    if url is not None:
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def test_lineage_has_one_baseline_and_explicit_incremental_history():
    scripts = ScriptDirectory.from_config(config_for())
    assert scripts.get_heads() == [PLATFORM_SCHEMA_REVISION]
    revisions = list(scripts.walk_revisions())
    assert [revision.revision for revision in revisions] == [PLATFORM_SCHEMA_REVISION, "0002_outbound_evidence", "0001_platform"]
    assert revisions[0].down_revision == "0002_outbound_evidence" and revisions[-1].down_revision is None


def test_fresh_initialization_matches_platform_contract(pg_url):
    url = fresh_database_url(pg_url)
    command.upgrade(config_for(url), "head")
    engine = create_engine(url)
    try:
        assert set(inspect(engine).get_table_names()) == TABLES | {"alembic_version"}
        with engine.connect() as connection:
            schema = connection.scalar(text("SELECT current_schema()"))
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == PLATFORM_SCHEMA_REVISION
            assert connection.scalar(text("SELECT count(*) FROM ai_project WHERE key='platform'")) == 1
            assert connection.scalar(text("SELECT count(*) FROM ai_api_key_config")) == 0
        assert upgrade_platform(engine, schema, apply=True)["changed"] is False
    finally:
        engine.dispose()


def test_initialization_refuses_nonempty_schema_without_touching_data(pg_url):
    url = fresh_database_url(pg_url)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE existing_data (id integer PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO existing_data VALUES (1)")
        with pytest.raises(RuntimeError, match="empty"):
            command.upgrade(config_for(url), "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM existing_data")) == 1
        assert "ai_task" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_unknown_revision_is_not_adopted_or_stamped(pg_url):
    url = fresh_database_url(pg_url)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            schema = connection.scalar(text("SELECT current_schema()"))
            connection.exec_driver_sql("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('unrecognized_revision')")
        with pytest.raises(ValueError, match="Unknown schema revision"):
            upgrade_platform(engine, schema, apply=True)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "unrecognized_revision"
    finally:
        engine.dispose()


def test_explicit_business_increment_preserves_previous_schema_data(pg_url):
    url = fresh_database_url(pg_url)
    command.upgrade(config_for(url), "0002_outbound_evidence")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            schema = connection.scalar(text("SELECT current_schema()"))
            previous = connection.scalar(text("SELECT count(*) FROM ai_project"))
        preview = upgrade_platform(engine, schema)
        assert preview["from_revision"] == "0002_outbound_evidence" and not preview["changed"]
        assert upgrade_platform(engine, schema, apply=True)["changed"]
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == PLATFORM_SCHEMA_REVISION
            assert connection.scalar(text("SELECT count(*) FROM ai_project")) == previous
        constraint = next(item for item in inspect(engine).get_check_constraints("ai_workflow_node")
                          if item["name"] == "ck_ai_workflow_node_kind")
        assert "step" in constraint["sqltext"] and "result" in constraint["sqltext"]
    finally:
        engine.dispose()


def test_baseline_cannot_be_discarded_by_downgrade(pg_url):
    url = fresh_database_url(pg_url)
    command.upgrade(config_for(url), "head")
    with pytest.raises(RuntimeError, match="cannot be discarded"):
        command.downgrade(config_for(url), "base")
    engine = create_engine(url)
    try:
        assert set(inspect(engine).get_table_names()) == TABLES | {"alembic_version"}
    finally:
        engine.dispose()
