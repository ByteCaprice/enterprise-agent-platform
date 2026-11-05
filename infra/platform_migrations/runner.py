"""Explicit incremental upgrade; never bootstrap/adopt an existing foreign schema."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from infra.platform_migrations import PLATFORM_SCHEMA_REVISION


def upgrade_platform(engine, schema, *, apply=False):
    if not schema or schema in {"public", "pg_catalog", "information_schema"} or schema.startswith("pg_"):
        raise ValueError("An isolated new-platform schema is required")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent))
    # The trusted target's actual ancestry is the allowlist. Do not maintain a
    # second revision list that silently loses the previous head on each release.
    lineage = {revision.revision for revision in ScriptDirectory.from_config(config).iterate_revisions(
        PLATFORM_SCHEMA_REVISION, "base",
    )}
    with engine.begin() as connection:
        actual = connection.scalar(text("SELECT current_schema()"))
        path = [item.strip().strip('"') for item in connection.scalar(text("SHOW search_path")).split(",")]
        if actual != schema or path != [schema, "pg_catalog"]:
            raise ValueError("Platform schema isolation check failed")
        # Same-schema upgrades serialize; other project/test schemas are independent.
        connection.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext(current_database()), hashtext(current_schema() || '/migrations'))"
        ))
        rows = connection.scalars(text("SELECT version_num FROM alembic_version")).all()
        if len(rows) != 1 or rows[0] not in lineage:
            raise ValueError("Unknown schema revision; implicit adoption is not supported")
        previous = rows[0]
        if apply and previous != PLATFORM_SCHEMA_REVISION:
            config.attributes["connection"] = connection
            command.upgrade(config, PLATFORM_SCHEMA_REVISION)
            if connection.scalar(text("SELECT version_num FROM alembic_version")) != PLATFORM_SCHEMA_REVISION:
                raise RuntimeError("Platform upgrade did not reach the expected revision")
        return {"schema": schema, "from_revision": previous, "target_revision": PLATFORM_SCHEMA_REVISION,
                "applied": apply, "changed": apply and previous != PLATFORM_SCHEMA_REVISION}
