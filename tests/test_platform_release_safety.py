"""Offline safety check for explicit backups; no pg_dump process or DB is used."""

import hashlib
import json
import stat
import tomllib
import zipfile
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

import pytest
from setuptools import find_namespace_packages
from setuptools.command.egg_info import FileList

from infra.platform_migrations import PLATFORM_SCHEMA_REVISION
from infra.release_checks import REQUIRED_ASSETS
from interfaces.platform_settings import load_settings
from scripts import backup_platform, build_platform_release


def test_backup_is_explicit_private_and_never_overwrites(tmp_path, monkeypatch):
    config = {"host": "synthetic.invalid", "name": "synthetic", "user": "fixture", "password": "synthetic-secret",
              "schema": "enterprise_ai_fixture"}
    monkeypatch.setattr(backup_platform, "load_settings", lambda _: {"database": config})
    monkeypatch.setattr(backup_platform, "build_engine", lambda _: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(backup_platform, "upgrade_platform", lambda *_args: {"from_revision": PLATFORM_SCHEMA_REVISION})
    calls = []

    def dump(command, **options):
        calls.append(command)
        assert "synthetic-secret" not in repr(command)
        assert options["env"]["PGPASSWORD"] == "synthetic-secret"
        assert "--schema=enterprise_ai_fixture" in command
        options["stdout"].write(b"synthetic-archive-only")

    monkeypatch.setattr(backup_platform.subprocess, "run", dump)
    output = tmp_path / "fixture.dump"
    assert backup_platform.backup("unused", output)["applied"] is False
    assert not output.exists() and not calls
    result = backup_platform.backup("unused", output, apply=True)
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    manifest = output.with_suffix(".dump.json")
    assert json.loads(manifest.read_text())["sha256"] == result["sha256"]
    assert "synthetic-secret" not in manifest.read_text()
    with pytest.raises(ValueError, match="overwrite"):
        backup_platform.backup("unused", output, apply=True)
    assert len(calls) == 1


def test_runtime_environment_timeouts_are_typed(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE__STATEMENT_TIMEOUT_MS", "15000")
    monkeypatch.setenv("DATABASE__LOCK_TIMEOUT_MS", "5000")
    monkeypatch.setenv("DATABASE__POOL_TIMEOUT_SECONDS", "10")
    database = load_settings(tmp_path)["database"]
    assert database["statement_timeout_ms"] == 15000
    assert database["lock_timeout_ms"] == 5000
    assert database["pool_timeout_seconds"] == 10


@pytest.mark.parametrize("path", ["business_agents/example.py", "unreviewed_samples/guide.md",
                                  "unreviewed_runtime/runner.py"])
def test_release_check_rejects_business_packages(tmp_path, path):
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for package, assets in REQUIRED_ASSETS.items():
            for asset in assets:
                archive.writestr(package.replace(".", "/") + "/" + asset, "")
        archive.writestr("test.dist-info/entry_points.txt", "enterprise-ai = interfaces.cli.platform:main")
        archive.writestr(path, "")
    with pytest.raises(ValueError, match="private or development"):
        build_platform_release.verify_wheel(wheel)


@pytest.mark.parametrize(
    "path",
    [
        "domain/secrets/provider_token.py",
        "domain/config/private_settings.py",
        "capability_hub/nested/.claude/session.py",
        "capability_hub/nested/.git-credentials",
        "capability_hub/nested/client.crt",
        "../outside.py",
        r"domain\\secrets\\token.py",
    ],
)
def test_release_check_rejects_nested_private_and_malformed_paths_without_disclosure(tmp_path, path):
    wheel = tmp_path / "private-fixture.whl"
    private_marker = "synthetic-secret-content-must-not-be-printed"
    with zipfile.ZipFile(wheel, "w") as archive:
        for package, assets in REQUIRED_ASSETS.items():
            for asset in assets:
                archive.writestr(package.replace(".", "/") + "/" + asset, "")
        archive.writestr("test.dist-info/entry_points.txt", "enterprise-ai = interfaces.cli.platform:main")
        archive.writestr(path, private_marker)
    with pytest.raises(ValueError, match="private or development") as error:
        build_platform_release.verify_wheel(wheel)
    assert private_marker not in str(error.value)


def test_release_check_allows_reviewed_runtime_assets(tmp_path):
    wheel = tmp_path / "runtime-fixture.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for package, assets in REQUIRED_ASSETS.items():
            for asset in assets:
                archive.writestr(package.replace(".", "/") + "/" + asset, "")
        archive.writestr("test.dist-info/entry_points.txt", "enterprise-ai = interfaces.cli.platform:main")
    assert build_platform_release.verify_wheel(wheel)["wheel"] == wheel.name


def test_manifest_exclusion_patterns_reject_synthetic_private_paths():
    required = [
        package.replace(".", "/") + "/" + asset
        for package, assets in REQUIRED_ASSETS.items()
        for asset in assets
    ]
    safe = {"README.md", "pyproject.toml", "uv.lock", *required}
    private = {
        "domain/private/token.py",
        "domain/nested/private/module.py",
        "domain/nested/secrets/token.py",
        "domain/nested/config/settings.py",
        "interfaces/http/console/private/module.py",
        "interfaces/http/console/.env.local",
        "framework/agent_runtime/pi/node/.git/config",
        "infra/_schema_worker.py.zip",
        "framework/agent_runtime/pi/node/trace.har",
        "orchestration/.local/settings.py",
        "capability_hub/remitter_search/secrets/token.py",
        "capability_hub/remitter_search/config/deployment_policy.py",
        "capability_hub/nested/deep/.claude/session.py",
        "capability_hub/nested/.codex/deep/session.py",
        "domain/nested/deep/config/settings.py",
        "domain/nested/.cursor/session.py",
        "interfaces/nested/.pi/session.py",
        "infra/nested/.git-credentials",
        "framework/nested/client.crt",
        "framework/nested/identity.pub",
        "orchestration/nested/workspace.iml",
    }
    file_list = FileList()
    file_list.files = sorted(safe | private)
    for raw_line in open("MANIFEST.in", encoding="utf-8"):
        line = raw_line.strip()
        if line and not line.startswith("#") and line.split(maxsplit=1)[0] in {
            "prune", "exclude", "recursive-exclude", "global-exclude",
        }:
            file_list.process_template_line(line)
    assert private.isdisjoint(file_list.files)
    assert safe.issubset(file_list.files)


def test_docker_final_denials_cover_private_files_inside_allowed_source_roots():
    lines = [line.strip() for line in Path("docker/platform/Dockerfile.dockerignore").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    final_denials = lines[max(index for index, line in enumerate(lines) if line.startswith("!")) + 1:]
    for path in (
        "capability_hub/nested/config/settings.py", "domain/nested/.claude/session.py",
        "framework/nested/.codex/session.py", "infra/nested/.cursor/session.py",
        "interfaces/nested/.pi/session.py", "orchestration/nested/.git-credentials",
        "capability_hub/nested/client.crt", "capability_hub/nested/identity.pub",
    ):
        assert any(fnmatchcase(path, pattern) for pattern in final_denials), path
    for package, assets in REQUIRED_ASSETS.items():
        for asset in assets:
            path = package.replace(".", "/") + "/" + asset
            assert not any(fnmatchcase(path, pattern) for pattern in final_denials), path


def test_package_discovery_excludes_nested_configuration(tmp_path):
    for name in ("domain", "domain/config", "capability_hub", "capability_hub/config", "capability_hub/weather_query"):
        package = tmp_path / name
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("")
    settings = tomllib.loads(Path("pyproject.toml").read_text())["tool"]["setuptools"]["packages"]["find"]
    packages = find_namespace_packages(where=str(tmp_path), include=settings["include"], exclude=settings["exclude"])
    assert {"domain", "capability_hub", "capability_hub.weather_query"}.issubset(packages)
    assert "domain.config" not in packages and "capability_hub.config" not in packages
