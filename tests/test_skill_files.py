"""Small Skill file import and CLI checks; no database or model calls."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_capability_hub import MemoryCapabilities

from infra.skill_files import MAX_SKILL_FILE_BYTES, SkillImportError, load_skill_bundle
from interfaces.cli import platform
from orchestration.control_plane_auth import ControlPlanePrincipal


def skill_file(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: weather-guide\ndescription: 天气结果说明\nallowed-tools: [bash]\n---\n\n只根据工具结果回答。\n")
    return path


def test_standard_skill_directory_imports_text_not_tools_or_sibling_assets(tmp_path):
    skill_file(tmp_path)
    (tmp_path / "run.py").write_text("raise RuntimeError('Must not execute')")
    bundle = load_skill_bundle(tmp_path, version="2")
    item, = bundle.capabilities
    assert (bundle.key, item.kind, item.version) == ("weather-guide", "skill", "2")
    assert item.spec.description == "天气结果说明"
    assert item.draft.definition == {"instructions": "只根据工具结果回答。"}
    assert item.draft.dependencies == [] and bundle.references == ()
    assert str(tmp_path) not in json.dumps(bundle.export())


def test_plain_markdown_accepts_explicit_key_and_version(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text("# Guide\nUse approved tools only.\n")
    bundle = load_skill_bundle(path, key="approved-guide", version="reviewed-1")
    assert bundle.key == "approved-guide" and bundle.version == "reviewed-1"
    assert bundle.capabilities[0].draft.definition["instructions"].startswith("# Guide")


@pytest.mark.parametrize("body", [
    "", "private instructions without a name", "---\nname: [private]\n---\nprivate body",
    "---\nname: weather-guide\nprivate malformed: [\n---\nbody",
    "---\nname: weather-guide\n---\n", "---\nname: weather-guide\n---\nprivate\x00body",
    "x" * (MAX_SKILL_FILE_BYTES + 1),
])
def test_invalid_files_report_no_private_content(tmp_path, body):
    path = tmp_path / "SKILL.md"
    path.write_text(body)
    with pytest.raises(SkillImportError) as caught:
        load_skill_bundle(path)
    assert str(caught.value) == "Invalid Skill Markdown or metadata"


def test_export_is_offline_and_produces_a_normal_skill_bundle(tmp_path, monkeypatch, capsys):
    source = skill_file(tmp_path)
    runtime = Mock(side_effect=AssertionError("Offline import opened deployment"))
    monkeypatch.setattr(platform, "create_runtime", runtime)
    platform.main(["skill-export", "--skill-file", str(source), "--skill-key", "custom-guide"])
    output = json.loads(capsys.readouterr().out)
    item, = output["capabilities"]
    assert item["spec"]["key"] == "custom-guide" and item["spec"]["kind"] == "skill"
    assert item["draft"]["definition"] == {"instructions": "只根据工具结果回答。"}
    runtime.assert_not_called()


def test_publish_uses_existing_capability_publication_and_authenticated_actor(tmp_path, monkeypatch, capsys):
    source = skill_file(tmp_path)
    repository = MemoryCapabilities()
    auth = SimpleNamespace(
        authenticate=Mock(return_value=ControlPlanePrincipal("skill-admin", frozenset({"admin"}))),
        allowed_resources=Mock(return_value={"*"}),
    )
    services = SimpleNamespace(store=SimpleNamespace(capabilities=repository), control_plane_auth=auth, close=Mock())
    monkeypatch.setattr(platform, "create_runtime", lambda _path: services)
    monkeypatch.setenv("SKILL_DEMO_ADMIN", "synthetic-secret")
    platform.main(["skill-publish", "--skill-file", str(source), "--project", "demo",
                   "--config-dir", "unused", "--api-key-env", "SKILL_DEMO_ADMIN"])
    artifact = repository.get_version("demo", "weather-guide", "1")
    assert artifact.status == "published" and artifact.published_by == "skill-admin"
    assert artifact.definition == {"instructions": "只根据工具结果回答。"}
    auth.authenticate.assert_called_once_with("synthetic-secret")
    services.close.assert_called_once()
    assert "synthetic-secret" not in capsys.readouterr().out


def test_invalid_skill_fails_before_initializing_deployment(tmp_path, monkeypatch, capsys):
    runtime = Mock(side_effect=AssertionError("Invalid import opened deployment"))
    monkeypatch.setattr(platform, "create_runtime", runtime)
    with pytest.raises(SystemExit) as caught:
        platform.main(["skill-publish", "--skill-file", str(tmp_path / "missing.md"),
                       "--project", "demo", "--config-dir", "unused"])
    assert caught.value.code == 1
    runtime.assert_not_called()
    assert str(tmp_path) not in capsys.readouterr().out
