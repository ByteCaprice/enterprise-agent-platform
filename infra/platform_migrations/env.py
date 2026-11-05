"""New-platform Alembic environment. An explicit non-public schema is mandatory."""

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

config = context.config


def migrate(connection, *, external=False):
    schema = connection.scalar(text("SELECT current_schema()"))
    search_path = connection.scalar(text("SHOW search_path"))
    path = [item.strip().strip('"') for item in search_path.split(",")]
    if (
        not schema or schema in {"public", "pg_catalog", "information_schema"}
        or schema.startswith("pg_") or path != [schema, "pg_catalog"]
    ):
        raise RuntimeError("Platform migrations refuse public or system schemas")
    if not external:
        connection.commit()
    context.configure(connection=connection, version_table_schema=schema)
    with context.begin_transaction():
        context.run_migrations()


def run():
    if context.is_offline_mode():
        raise RuntimeError("Platform migrations require an explicit online schema isolation check")
    supplied = config.attributes.get("connection")
    if supplied is not None:
        migrate(supplied, external=True)
        return
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("Platform migration database URL is required")
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()


run()
