"""Offline repository boundaries; never stage files or inspect another project."""

import json
import re
import subprocess
from pathlib import Path

from jsonschema import Draft202012Validator

from domain.capability import CapabilityVersionDraft
from domain.workflow import WorkflowDefinition
from framework.workflow import apply_rule, map_values
from scripts import check_repository_files as audit
from scripts.check_repository_files import PROTECTED_PATHS, SOURCE_PATHS, check, ignored_paths
from scripts.check_repository_files import ROOT as PROJECT_ROOT
from scripts.repository_policy import private_distribution_path, private_repository_path


def test_private_files_are_ignored_without_hiding_runtime_assets():
    assert ignored_paths(PROTECTED_PATHS) == set(PROTECTED_PATHS)
    assert not ignored_paths(SOURCE_PATHS)


def test_private_identity_and_assistant_files_are_excluded_from_all_distribution_policies():
    paths = (
        "domain/.gitconfig", "framework/.git-credentials", "infra/server.crt", "interfaces/client.cer",
        "orchestration/id.pub", "domain/.claude/settings.json", "framework/.codex/session.py",
        "infra/.cursor/rules.py", "interfaces/.pi/session.py", "tests/.aider.chat.history.md",
        "domain/AGENTS.md", "infra/CLAUDE.md", "interfaces/GEMINI.md", "framework/workspace.iml",
        "orchestration/project.code-workspace",
    )
    assert ignored_paths(paths) == set(paths)
    for path in paths:
        assert private_repository_path(path), path
        assert private_distribution_path(path), path


def test_capability_hub_allows_public_code_but_not_private_resources():
    public = (
        "capability_hub/catalog.py", "capability_hub/remitter_search/workflow.py",
        "capability_hub/weather_query/tools.py",
    )
    private = (
        "capability_hub/private/settings.py", "capability_hub/config/settings.py", "capability_hub/weather_query/.env.local",
        "capability_hub/remitter_search/customer.csv", "capability_hub/.claude/settings.json",
        "capability_hub/deployment.key", "capability_hub/internal-notes.md",
    )
    assert not ignored_paths(public)
    assert ignored_paths(private) == set(private)
    for path in public:
        assert not private_repository_path(path), path
        assert not private_distribution_path(path), path
    for path in private:
        assert private_repository_path(path), path
        assert private_distribution_path(path), path


def test_repository_document_links_and_upload_candidates_are_clean():
    result = check()
    assert result["findings"] == [], result["findings"]


def test_temporary_parent_index_is_scoped_redacted_and_does_not_follow_symlinks(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("HOME", str(tmp_path))
    parent = tmp_path / "parent"
    project = parent / "project"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    token = "sk-" + "a" * 40
    (project / "domain").mkdir()
    (project / "domain/sample.py").write_text(token)
    (parent / "sibling.py").write_text(token)
    target = tmp_path / "outside.txt"
    target.write_text(token)
    (project / "domain/link.py").symlink_to(target)
    subprocess.run(["git", "add", "--", "project", "sibling.py"], cwd=parent, check=True)
    (project / "domain/sample.py").write_text("clean\n")
    monkeypatch.setattr(audit, "ROOT", project)
    monkeypatch.setattr(audit, "PROTECTED_PATHS", ())
    monkeypatch.setattr(audit, "SOURCE_PATHS", ())
    monkeypatch.setattr(audit, "PUBLIC_MARKDOWN", ())

    findings = audit.check()["findings"]
    assert { (item["code"], item["path"], item.get("source")) for item in findings } == {
        ("POSSIBLE_LITERAL_CREDENTIAL", "domain/sample.py", "index"),
        ("INDEX_ENTRY_REQUIRES_REVIEW", "domain/link.py", "index"),
        ("SYMLINK_REQUIRES_REVIEW", "domain/link.py", None),
    }
    assert token not in json.dumps(findings)


def test_document_directory_symlink_does_not_read_target(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "api.md").write_text("synthetic outside content")
    (project / "docs").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("HOME", str(tmp_path))
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    monkeypatch.setattr(audit, "ROOT", project)
    monkeypatch.setattr(audit, "PROTECTED_PATHS", ())
    monkeypatch.setattr(audit, "SOURCE_PATHS", ())
    monkeypatch.setattr(audit, "PUBLIC_MARKDOWN", ("docs/api.md",))
    reads = []
    original = Path.read_text

    def record_read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", record_read)
    audit.check()
    assert not reads, "public-document scan followed a directory symlink"


def test_manifest_provenance_and_wheel_boundary_are_redacted():
    name = "framework/agent_runtime/pi/node/package-lock.json"
    url = "https://user:password@internal.example/pkg.tgz?token=secret"
    bad = audit.scan_blob(name, json.dumps({"resolved": url}).encode(), source="index")
    assert {item["code"] for item in bad} == {"DEPENDENCY_SOURCE_REQUIRES_REVIEW", "DEPENDENCY_URL_CREDENTIAL"}
    assert url not in json.dumps(bad) and "password" not in json.dumps(bad)
    good = {"resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz"}
    assert audit.scan_blob(name, json.dumps(good).encode(), source="index") == []
    assert audit.scan_blob("uv.lock", b'[[package]]\nsource = { registry = "https://pypi.org/simple" }\n', source="index") == []
    assert private_distribution_path("config/private.yaml")
    assert not private_distribution_path(name)
    direct = f'[project]\ndependencies = ["example @ {url}"]\n'
    findings = audit.scan_blob("pyproject.toml", direct.encode(), source="index")
    assert {item["code"] for item in findings} == {"DEPENDENCY_SOURCE_REQUIRES_REVIEW", "DEPENDENCY_URL_CREDENTIAL"}
    assert url not in json.dumps(findings) and "password" not in json.dumps(findings)
    npm_url = "https://internal.example/private.tgz"
    npm_data = json.dumps({"dependencies": {"example": npm_url}}).encode()
    findings = audit.scan_blob("framework/agent_runtime/pi/node/package.json", npm_data, source="index")
    assert {item["code"] for item in findings} == {"DEPENDENCY_SOURCE_REQUIRES_REVIEW"} and npm_url not in json.dumps(findings)
    for template, blank, populated in (
        ("docker/platform/app.env.example", "DATABASE__HOST=", "DATABASE__HOST=synthetic-private-host"),
        ("docker/platform/deployment-policy.example.json", '"owner": ""', '"owner": "synthetic-private-owner"'),
    ):
        data = (PROJECT_ROOT / template).read_bytes()
        assert audit.scan_blob(template, data, source="working_tree") == []
        findings = audit.scan_blob(template, data.replace(blank.encode(), populated.encode()), source="index")
        assert {item["code"] for item in findings} == {"PUBLIC_TEMPLATE_NOT_EMPTY"} and "synthetic-private" not in json.dumps(findings)


def test_documented_first_workflow_uses_real_contracts_and_rules():
    guide = (PROJECT_ROOT / "docs" / "api.md").read_text(encoding="utf-8")
    examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", guide, flags=re.DOTALL)]
    draft = CapabilityVersionDraft.model_validate(next(value for value in examples if "definition" in value))
    definition = WorkflowDefinition.model_validate(draft.definition)
    task_input, outputs = {"message": "hello"}, {}
    Draft202012Validator(draft.input_schema).validate(task_input)
    for node in definition.ordered_nodes():
        assert node.kind == "rule" and node.when is None and node.rule is not None
        values = map_values(node.input_mapping, task_input, outputs)
        outputs[node.key] = apply_rule(node.rule, values)
    result = map_values(definition.output_mapping, task_input, outputs)
    assert result == {"message": "hello"}
    Draft202012Validator(draft.output_schema).validate(result)
