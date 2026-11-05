"""The platform schema is explicit, self-contained and matches its frozen baseline."""

import ast
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

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


def test_frozen_baseline_covers_current_columns_constraints_and_indexes():
    sql = (Path(__file__).resolve().parents[1] / "infra/platform_migrations/versions/schema.sql").read_text()
    sql = " ".join(sql.split())
    for table in platform_metadata.sorted_tables:
        assert " ".join((str(CreateTable(table).compile(dialect=postgresql.dialect())).strip() + ";").split()) in sql
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
