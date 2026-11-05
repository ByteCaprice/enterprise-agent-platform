"""Offline package asset checks, shared by installed CLI and wheel validation."""

from importlib.resources import files

from infra.platform_migrations import PLATFORM_SCHEMA_REVISION

REQUIRED_ASSETS = {
    "infra": ("_schema_worker.py",),
    "interfaces.http": ("console/index.html", "console/app.js", "console/app.css", "console/icon.svg"),
    "infra.platform_migrations": (
        f"versions/{PLATFORM_SCHEMA_REVISION}.py", "versions/schema.sql",
    ),
    "framework.agent_runtime.pi": ("node/platform-runner.mjs", "node/package.json", "node/package-lock.json"),
}


def check_installed_assets():
    for package, paths in REQUIRED_ASSETS.items():
        for path in paths:
            if not files(package).joinpath(path).is_file():
                raise RuntimeError("Installed release is missing required runtime assets")
    return {"status": "ok", "schema_revision": PLATFORM_SCHEMA_REVISION, "assets_checked": sum(map(len, REQUIRED_ASSETS.values()))}
