"""Explicit isolated-schema pg_dump, never restore, migrate or overwrite an archive.

Default only validates the configured schema/lineage. Credentials are passed in
the child environment, never command-line arguments or printed driver errors.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from infra.database import build_engine
from infra.platform_migrations.runner import upgrade_platform
from interfaces.platform_settings import load_settings


def backup(config_dir, output=None, *, apply=False):
    config = load_settings(config_dir).get("database", {})
    schema = config.get("schema")
    if not isinstance(schema, str) or not re.fullmatch(r"enterprise_ai(?:_[a-z0-9_]{1,40})?", schema):
        raise ValueError("Backup requires an isolated enterprise_ai schema")
    engine = build_engine(config)
    try:
        preflight = upgrade_platform(engine, schema)
    finally:
        engine.dispose()
    result = {"schema": schema, "revision": preflight["from_revision"], "applied": False}
    if not apply:
        return result
    if output is None:
        raise ValueError("An explicit new backup archive is required")
    output = Path(output).resolve()
    manifest = output.with_suffix(output.suffix + ".json")
    if output.exists() or manifest.exists():
        raise ValueError("Refusing to overwrite a backup or manifest")
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "LC_ALL"}}
    env.update({"PGHOST": str(config["host"]), "PGPORT": str(config.get("port", 5432)), "PGDATABASE": str(config["name"]),
        "PGUSER": str(config["user"]), "PGPASSWORD": str(config["password"]),
        "PGSSLMODE": str(config.get("sslmode", "prefer")), "PGCONNECT_TIMEOUT": "10"})
    if config.get("sslrootcert"):
        env["PGSSLROOTCERT"] = str(config["sslrootcert"])
    # A failed dump remains an incomplete archive without a success manifest.
    # Never delete operator files or expose pg_dump stderr (which may quote data).
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as archive:
        subprocess.run(["pg_dump", "--format=custom", "--strict-names", f"--schema={schema}", "--no-owner", "--no-acl"],
                       env=env, stdout=archive, stderr=subprocess.PIPE, check=True, timeout=3600)
    digest = hashlib.sha256()
    with output.open("rb") as archive:
        while chunk := archive.read(1024 * 1024):
            digest.update(chunk)
    result.update({"applied": True, "archive": output.name, "sha256": digest.hexdigest(),
                   "restore_scope": "separate approved empty database only; never restore over a live schema"})
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(result, handle, indent=2)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.out is None:
        parser.error("--apply requires --out")
    try:
        print(json.dumps(backup(args.config_dir, args.out, apply=args.apply)))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "partial_archive_must_not_be_restored": True}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
