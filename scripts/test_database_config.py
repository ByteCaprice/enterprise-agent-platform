"""Parse an explicitly selected SIT/UAT database block; never connect or print values."""

import re
from pathlib import Path
from urllib.parse import urlsplit


def read_test_database(path: Path, environment: str = "SIT") -> dict:
    """Reject missing/ambiguous blocks, production sections and VPC-only addresses."""
    if environment not in {"SIT", "UAT"}:
        raise ValueError("Only explicit SIT or UAT databases are permitted")
    lines = path.read_text(encoding="utf-8").splitlines()
    heading = re.compile(
        r"(?<![A-Za-z0-9_])(SIT|UAT|UIT|PRODUCTION|PROD|生产环境|生产数据库|正式环境)(?![A-Za-z0-9_])", re.I,
    )
    sections = [
        (i, match.group(1).upper())
        for i, line in enumerate(lines)
        if (match := heading.search(re.split(r"[:：=]", line, maxsplit=1)[0]))
    ]
    starts = [i for i, name in sections if name == environment]
    if len(starts) > 1:
        starts = [i for i in starts if re.search(r"数据库|database", lines[i], re.I)]
    if len(starts) != 1:
        raise ValueError(f"Exactly one explicit {environment} database section is required")
    start = starts[0]
    end = next((i for i, _ in sections if i > start), len(lines))
    database = {}
    for line in lines[start + 1:end]:
        if not line.strip() and all(database.get(key) for key in ("host", "name", "user", "password")):
            break
        parts = re.split(r"[:：=]", line.strip(), maxsplit=1)
        if len(parts) != 2:
            continue
        key, value = parts[0].strip().lower(), parts[1].strip()
        if "vpc" in key:
            continue
        field = None
        if "地址" in key or key in {"host", "database__host"}:
            field = "host"
        elif "端口" in key or key in {"port", "database__port"}:
            field = "port"
        elif "密码" in key or key in {"password", "database__password"}:
            field = "password"
        elif "用户" in key or "账号" in key or key in {"user", "username", "database__user"}:
            field = "user"
        elif "库" in key or key in {"name", "dbname", "database", "database__name"}:
            field = "name"
        if field is not None:
            if field in database:
                raise ValueError(f"Duplicate {environment} connection fields are not allowed")
            database[field] = int(value) if field == "port" else value
    if not all(database.get(key) for key in ("host", "name", "user", "password")):
        raise ValueError(f"{environment} connection fields are incomplete")
    host = urlsplit("//" + database["host"])
    if not host.hostname or host.path or host.query or host.username or host.password:
        raise ValueError(f"Invalid {environment} host format")
    database["host"] = host.hostname
    if host.port is not None and database.get("port", host.port) != host.port:
        raise ValueError(f"Conflicting {environment} port settings")
    database.setdefault("port", host.port or 5432)
    if not 1 <= database["port"] <= 65535:
        raise ValueError(f"Invalid {environment} port")
    return database
