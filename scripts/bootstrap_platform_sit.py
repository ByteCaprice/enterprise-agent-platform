"""Create a fresh Enterprise AI SIT schema; never reuse the old project's tables.

Default is a read-only preflight. --apply creates the named new schema, runs
Alembic there, and initializes only new platform configuration. No legacy seed,
business records, external HTTP requests or model calls are performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL

from domain import ApiKeyConfig
from infra.database import build_engine
from infra.platform_store import PlatformStore
from scripts.test_database_config import read_test_database

ROOT = Path(__file__).resolve().parents[1]


def validate_schema(schema: str) -> str:
    if re.fullmatch(r"enterprise_ai(?:_[a-z0-9_]{1,40})?", schema) is None:
        raise ValueError("New platform schema must be enterprise_ai or enterprise_ai_<suffix>")
    return schema


def _private_file(path: Path, content: str) -> None:
    # Refuse to overwrite any pre-existing operator configuration or credential.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def bootstrap(config_file: Path, schema: str, config_dir: Path, *, apply: bool = False) -> dict:
    schema = validate_schema(schema)
    database = read_test_database(config_file, "SIT")
    base = URL.create(
        "postgresql+psycopg2", host=database["host"], port=database["port"], database=database["name"],
        username=database["user"], password=database["password"],
    )
    admin = create_engine(base, connect_args={"connect_timeout": 10})
    try:
        with admin.connect() as connection:
            exists = connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=:schema)"),
                                       {"schema": schema})
        if exists:
            raise ValueError("Target schema already exists; refusing to mix data or overwrite it")
        if not apply:
            return {"environment": "SIT", "schema": schema, "exists": False, "applied": False}
        if config_dir.exists() and any(config_dir.iterdir()):
            raise ValueError("Target local config directory is not empty")
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(config_dir, 0o700)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()

    scoped = base.update_query_dict({"options": f"-csearch_path={schema},pg_catalog"})
    alembic = Config()
    alembic.set_main_option("script_location", str(ROOT / "infra" / "platform_migrations"))
    alembic.set_main_option("sqlalchemy.url", scoped.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(alembic, "head")

    token = secrets.token_urlsafe(32)
    digest = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
    store = PlatformStore(build_engine({**database, "schema": schema}))
    try:
        store.api_key_configs.save(ApiKeyConfig(
            name="enterprise-ai-bootstrap-admin", api_key=digest, scopes=["*"],
        ))
        with store.engine.connect() as connection:
            if connection.scalar(text("SELECT current_schema()")) != schema:
                raise RuntimeError("Platform database schema isolation check failed")
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        tables = inspect(store.engine).get_table_names()
    finally:
        store.close()

    env = {
        "DATABASE__BACKEND": "postgresql", "DATABASE__HOST": database["host"],
        "DATABASE__PORT": database["port"], "DATABASE__NAME": database["name"],
        "DATABASE__USER": database["user"], "DATABASE__PASSWORD": database["password"],
        "DATABASE__SCHEMA": schema, "DATABASE__STRICT_PG": True,
        "SERVER__HOST": "127.0.0.1", "SERVER__PORT": 50020,
        "LOGGING__FILE_ENABLED": False,
    }
    _private_file(config_dir / ".env", "".join(f"{key}={json.dumps(str(value), ensure_ascii=False)}\n" for key, value in env.items()))
    _private_file(config_dir / ".env.local", f"ENTERPRISE_AI_ADMIN_KEY={token}\n")
    return {"environment": "SIT", "schema": schema, "applied": True, "revision": revision,
            "tables": len(tables), "config_dir": str(config_dir), "admin_key_storage": "sha256 digest only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = bootstrap(args.config, args.schema, args.config_dir, apply=args.apply)
    except Exception as exc:
        # Never dump credential-bearing SQLAlchemy/driver exception text.
        print(f"Platform SIT bootstrap failed ({type(exc).__name__}); inspect schema before retrying")
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
