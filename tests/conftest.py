"""New-platform fixtures. Database tests require an explicit private SIT config.

No Docker, business seeds, model calls or existing-schema writes.
Offline tests do not initialize a database. Each database session owns randomly
named schemas and may clean up only those exact schemas.
"""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

_PG: dict = {}
_STORES: list = []


def pytest_addoption(parser):
    parser.addoption("--sit-config", default=None,
                     help="Explicit private SIT config; only disposable session-owned schemas are modified.")


def pytest_collection_modifyitems(items):
    for item in items:
        if "pg_url" in item.fixturenames:
            item.add_marker(pytest.mark.database)


def fresh_database_url(_url: str) -> str:
    """Create a session-owned schema, with no public search-path fallback."""
    if not _PG:
        raise RuntimeError("Database tests require the pg_url fixture and --sit-config")
    schema = f"platform_test_{uuid4().hex}"
    base = make_url(_PG["base_url"])
    engine = create_engine(base, connect_args={"connect_timeout": 10}, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        _PG["schemas"].append(schema)
    finally:
        engine.dispose()
    return base.update_query_dict({"options": f"-csearch_path={schema},pg_catalog -cstatement_timeout=30000"}).render_as_string(
        hide_password=False,
    )


def _drop_test_schemas():
    engine = create_engine(_PG["base_url"], connect_args={"connect_timeout": 10}, poolclass=NullPool)
    try:
        for schema in reversed(_PG["schemas"]):
            if not schema.startswith("platform_test_") or len(schema) != 46:
                raise RuntimeError("Refusing to clean an unrecognized test schema")
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()
        _PG.clear()


@pytest.fixture(scope="session")
def _test_database(request):
    config_path = request.config.getoption("--sit-config")
    if not config_path:
        pytest.skip("Database tests require explicit --sit-config; no database was contacted")
    from infra.store.model import platform_metadata
    from scripts.test_database_config import read_test_database

    config = read_test_database(Path(config_path), "SIT")
    base = URL.create("postgresql+psycopg2", host=config["host"], port=config["port"],
                      username=config["user"], password=config["password"], database=config["name"])
    _PG.update(base_url=base.render_as_string(hide_password=False), schemas=[], **config)
    try:
        _PG["url"] = fresh_database_url(_PG["base_url"])
        _PG["schema"] = _PG["schemas"][0]
        engine = create_engine(_PG["url"], poolclass=NullPool)
        try:
            platform_metadata.create_all(engine)
        finally:
            engine.dispose()
        yield
    finally:
        _drop_test_schemas()


@pytest.fixture
def pg_url(_test_database):
    return _PG["url"]


@pytest.fixture(autouse=True)
def _isolate_platform_tables(request):
    if "pg_url" not in request.fixturenames:
        yield
        return
    request.getfixturevalue("pg_url")
    from infra.store.model import platform_metadata

    engine = create_engine(_PG["url"], poolclass=NullPool)
    try:
        with engine.begin() as conn:
            if conn.scalar(text("SELECT current_schema()")) != _PG["schema"]:
                raise RuntimeError("Refusing test cleanup outside its isolated schema")
            tables = ", ".join(f'"{_PG["schema"]}"."{name}"' for name in platform_metadata.tables)
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        yield
    finally:
        for store in _STORES:
            store.close()
        _STORES.clear()
        engine.dispose()


def make_store():
    """Create only a PlatformStore in the active test schema."""
    if not _PG:
        raise RuntimeError("Use the pg_url fixture before creating a test store")
    from infra.platform_store import PlatformStore

    store = PlatformStore(create_engine(_PG["url"], poolclass=NullPool))
    _STORES.append(store)
    return store


def database_settings() -> dict:
    """Connection settings for the disposable schema, never a private-file default."""
    if not _PG:
        raise RuntimeError("Use the pg_url fixture before reading test database settings")
    return {"backend": "postgresql", **{key: _PG[key] for key in ("host", "port", "user", "password", "name")},
            "sslmode": "prefer", "schema": _PG["schema"]}
