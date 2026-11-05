"""PostgreSQL engine construction; schema lifecycle is explicit and external."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import URL


def build_engine(config: dict[str, Any]):
    """Build a PostgreSQL engine from a ``database`` settings dictionary.

    Schema management is out-of-band; this only opens a connection pool.
    """
    user = config.get("user") or config.get("username") or ""
    password = config.get("password") or ""
    host = config.get("host", "localhost")
    port = int(config.get("port", 5432))
    dbname = config.get("name") or config.get("database") or config.get("dbname") or ""
    sslmode = config.get("sslmode", "prefer")

    url = URL.create(
        "postgresql+psycopg2",
        username=user or None,
        password=password or None,
        host=host,
        port=port,
        database=dbname,
        query={"sslmode": sslmode, **({"sslrootcert": config["sslrootcert"]} if config.get("sslrootcert") else {})},
    )
    connect_args: dict[str, Any] = {"connect_timeout": int(config.get("connect_timeout_seconds", 10))}
    schema = config.get("schema")
    if schema is not None:
        if not isinstance(schema, str) or re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema) is None:
            raise ValueError("database.schema must be a lowercase PostgreSQL identifier")
        # No public fallback: isolated schemas must never access public tables.
        connect_args["options"] = f"-csearch_path={schema},pg_catalog"
    for setting in ("statement_timeout", "lock_timeout"):
        value = config.get(setting + "_ms")
        if value is not None:
            if type(value) is not int or not 1 <= value <= 3600000:
                raise ValueError("Database timeout must be a bounded positive integer")
            connect_args["options"] = connect_args.get("options", "") + f" -c{setting}={value}"
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=int(config.get("pool_max_size", config.get("max_connections", 10))),
        max_overflow=0,
        pool_timeout=int(config.get("pool_timeout_seconds") or 30),
        connect_args=connect_args,
    )
