"""The schema is explicit and self-contained, with a frozen baseline and increments."""

import ast
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from domain.governance import CapabilityAction
from infra.store.model import platform_metadata


def test_platform_metadata_matches_frozen_migration_tables():
    versions = Path(__file__).resolve().parents[1] / "infra" / "platform_migrations" / "versions"
    migration_tables = set()
    for sql in versions.glob("*.sql"):
        migration_tables.update(re.findall(r"\bCREATE\s+TABLE\s+(ai_\w+)", sql.read_text(), re.IGNORECASE))
    for source in versions.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create_table"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                migration_tables.add(node.args[0].value)

    assert set(platform_metadata.tables) == migration_tables


def test_platform_foreign_keys_resolve_only_within_platform_metadata():
    for table in platform_metadata.sorted_tables:
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table.metadata is platform_metadata


def test_persisted_resource_grant_actions_match_the_domain_contract():
    constraint = next(item for item in platform_metadata.tables["ai_resource_grant"].constraints
                      if item.name == "ck_ai_resource_grant_action")
    assert set(re.findall(r"'([^']+)'", str(constraint.sqltext))) == {action.value for action in CapabilityAction}


def test_frozen_baseline_plus_explicit_increment_covers_columns_constraints_and_indexes():
    sql = (Path(__file__).resolve().parents[1] / "infra/platform_migrations/versions/schema.sql").read_text()
    sql = " ".join(sql.split())
    for table in platform_metadata.sorted_tables:
        ddl = " ".join((str(CreateTable(table).compile(dialect=postgresql.dialect())).strip() + ";").split())
        if table.name == "ai_platform_model_call":
            # 0002 is additive; the frozen 0001 DDL must remain unchanged.
            column = "outbound_evidence JSONB, "
            constraint = (", CONSTRAINT ck_ai_platform_model_call_outbound_evidence CHECK "
                          "(outbound_evidence IS NULL OR jsonb_typeof(outbound_evidence) = 'object')")
            assert column in ddl and constraint in ddl
            ddl = ddl.replace(column, "").replace(constraint, "")
        if table.name == "ai_resource_grant":
            assert "'agent:invoke','workflow:invoke'," in ddl
            ddl = ddl.replace("'agent:invoke','workflow:invoke',", "'agent:invoke',")
        if table.name == "ai_workflow_node":
            # 0003 adds scoped, hierarchical activity evidence; 0001 stays frozen.
            additions = (
                "parent_node_id TEXT, ", "scope JSONB, ",
                ", CONSTRAINT fk_ai_workflow_node_parent FOREIGN KEY(task_id, task_attempt_id, parent_node_id) REFERENCES ai_workflow_node (task_id, task_attempt_id, id)",
                ", CONSTRAINT ck_ai_workflow_node_parent CHECK (parent_node_id IS NULL OR parent_node_id != id)",
                ", CONSTRAINT ck_ai_workflow_node_scope CHECK (scope IS NULL OR jsonb_typeof(scope) = 'object')",
            )
            for addition in additions:
                assert addition in ddl
                ddl = ddl.replace(addition, "")
            expanded = {
                "kind IN ('rule','agent','tool','llm','step','result','workflow')": "kind IN ('rule','agent','tool','llm')",
                "status IN ('running','succeeded','failed','skipped','unavailable','cancelled')": "status IN ('running','succeeded','failed','skipped','cancelled')",
                "status IN ('failed','cancelled','unavailable')": "status IN ('failed','cancelled')",
            }
            for current, baseline in expanded.items():
                assert current in ddl
                ddl = ddl.replace(current, baseline)
        assert ddl in sql
        for index in table.indexes:
            assert " ".join((str(CreateIndex(index).compile(dialect=postgresql.dialect())).strip() + ";").split()) in sql


def test_platform_store_exposes_only_explicit_composition():
    code = """
import sys
import infra.platform_store
from infra.platform_store import PlatformStore
assert callable(PlatformStore.unit_of_work)
assert not hasattr(PlatformStore, '__getattr__')
assert 'mcp' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True)
