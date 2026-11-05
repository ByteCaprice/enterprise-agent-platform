"""Read-only distribution audit, scoped to this project even in a parent Git repo.

Inspects working-tree candidates AND index blobs, without staging, deleting,
following symlinks, fetching URLs or printing possible secret values. Not a
complete secret scanner or an authorization to publish.
"""

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from scripts.repository_policy import (
    PROTECTED_PATHS,
    PUBLIC_MARKDOWN,
    SOURCE_PATHS,
    private_repository_path,
)

ROOT = Path(__file__).resolve().parents[1]
MAX_SCAN_BYTES = 3_000_000
TOKEN = re.compile(r"(?<![\w-])(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{24,}|glpat-[A-Za-z0-9_-]{18,})")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{64,}")
PUBLIC_PACKAGE_HOSTS = {"pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com", "codeload.github.com"}
SENSITIVE_QUERY = {"token", "access_token", "auth_token", "key", "api_key", "apikey", "api-key", "password",
                   "auth", "authorization", "secret", "client_secret", "signature", "x-amz-signature", "x-amz-credential"}
MANIFESTS = {"uv.lock", "pyproject.toml", "framework/agent_runtime/pi/node/package.json",
             "framework/agent_runtime/pi/node/package-lock.json"}


def git_paths(*args):
    result = subprocess.run(["git", *args, "-z", "--", "."], cwd=ROOT, capture_output=True, check=True)
    return {name.decode("utf-8") for name in result.stdout.split(b"\0") if name}


def ignored_paths(paths):
    paths = tuple(paths)
    if not paths:
        return set()
    result = subprocess.run(["git", "check-ignore", "--no-index", "-z", "--stdin"], cwd=ROOT,
        input=b"\0".join(name.encode() for name in paths) + b"\0", capture_output=True)
    if result.returncode not in {0, 1}:
        raise RuntimeError("Cannot inspect Git ignore rules")
    return {name.decode("utf-8") for name in result.stdout.split(b"\0") if name}


def _manifest_findings(name, text, source):
    if name not in MANIFESTS:
        return []
    try:
        document = json.loads(text) if name.endswith(".json") else tomllib.loads(text)
    except (ValueError, TypeError):
        return [{"code": "DEPENDENCY_MANIFEST_INVALID", "path": name, "source": source}]
    findings = []
    pending = [document]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key == "source" and isinstance(value, dict):
                    if any(field in value and value[field] != "." for field in ("editable", "path", "directory", "virtual")):
                        findings.append({"code": "DEPENDENCY_LOCAL_SOURCE", "path": name, "source": source})
                if key in {"dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "overrides", "resolutions"} and isinstance(value, dict) and name.endswith(".json"):
                    versions = list(value.values())
                    while versions:
                        version = versions.pop()
                        if isinstance(version, dict):
                            versions.extend(version.values())
                        elif isinstance(version, str):
                            if version.startswith(("file:", "link:", "workspace:", "../", "./", "/")):
                                findings.append({"code": "DEPENDENCY_LOCAL_SOURCE", "path": name, "source": source})
                            elif re.match(r"[A-Za-z][A-Za-z0-9+.-]*://|git@", version):
                                try:
                                    dependency = urlsplit(version)
                                    if dependency.scheme != "https" or dependency.hostname not in PUBLIC_PACKAGE_HOSTS:
                                        findings.append({"code": "DEPENDENCY_SOURCE_REQUIRES_REVIEW", "path": name, "source": source})
                                except ValueError:
                                    findings.append({"code": "DEPENDENCY_SOURCE_INVALID", "path": name, "source": source})
                if key in {"url", "registry", "resolved", "git"} and isinstance(value, str):
                    try:
                        parsed = urlsplit(value)
                        if key in {"registry", "resolved", "git"} or key == "url" and name in {"uv.lock", "pyproject.toml"}:
                            if parsed.scheme != "https" or parsed.hostname not in PUBLIC_PACKAGE_HOSTS:
                                findings.append({"code": "DEPENDENCY_SOURCE_REQUIRES_REVIEW", "path": name, "source": source})
                        if parsed.username or parsed.password or any(
                            field.lower() in SENSITIVE_QUERY for field, _ in parse_qsl(parsed.query) + parse_qsl(parsed.fragment)
                        ):
                            findings.append({"code": "DEPENDENCY_URL_CREDENTIAL", "path": name, "source": source})
                    except ValueError:
                        findings.append({"code": "DEPENDENCY_SOURCE_INVALID", "path": name, "source": source})
                pending.append(value)
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            # PEP 508 direct dependencies are strings, not source/url dictionaries.
            if name == "pyproject.toml" and re.search(r"\s@\s*(?:file:|/|\.{1,2}/|[A-Za-z]:[\\/])", item):
                findings.append({"code": "DEPENDENCY_LOCAL_SOURCE", "path": name, "source": source})
            for value in re.findall(r"(?:https?|git\+https?|ssh)://[^\s\"'<>]+", item):
                try:
                    parsed = urlsplit(value)
                    if name == "pyproject.toml" and (parsed.scheme != "https" or parsed.hostname not in PUBLIC_PACKAGE_HOSTS):
                        findings.append({"code": "DEPENDENCY_SOURCE_REQUIRES_REVIEW", "path": name, "source": source})
                    if parsed.username or parsed.password or any(
                        field.lower() in SENSITIVE_QUERY for field, _ in parse_qsl(parsed.query) + parse_qsl(parsed.fragment)
                    ):
                        findings.append({"code": "DEPENDENCY_URL_CREDENTIAL", "path": name, "source": source})
                except ValueError:
                    findings.append({"code": "DEPENDENCY_SOURCE_INVALID", "path": name, "source": source})
    return list({tuple(sorted(item.items())): item for item in findings}.values())


def _template_findings(name, text, source):
    if name == "docker/platform/deployment-policy.example.json":
        try:
            template = json.loads(text)
            fields = {"owner", "identity_policy_ref", "financial_policy_ref", "retention_policy_ref",
                      "backup_restore_ref", "incident_runbook_ref", "edge_security_ref"}
            if (not isinstance(template, dict) or set(template) != fields | {"shared_edge_limits_confirmed"} or
                    any(template[field] != "" for field in fields) or template["shared_edge_limits_confirmed"] is not False):
                return [{"code": "PUBLIC_TEMPLATE_NOT_EMPTY", "path": name, "source": source}]
        except (ValueError, TypeError):
            return [{"code": "PUBLIC_TEMPLATE_INVALID", "path": name, "source": source}]
    if name == "docker/platform/app.env.example":
        empty_fields = {"DATABASE__HOST", "DATABASE__NAME", "DATABASE__USER", "DATABASE__PASSWORD"}
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().upper()
            sensitive = key in empty_fields or any(word in key for word in ("PASSWORD", "TOKEN", "SECRET", "API_KEY"))
            if sensitive and value.strip().strip("\"'"):
                return [{"code": "PUBLIC_TEMPLATE_NOT_EMPTY", "path": name, "source": source, "line": number}]
    return []


def has_symlink_component(path):
    """Check parents before touching a child; never resolve a link target."""
    try:
        parts = path.relative_to(ROOT).parts
    except ValueError:
        return True
    current = ROOT
    if current.is_symlink():
        return True
    for part in parts:
        if part == "..":
            return True
        current = current / part
        if current.is_symlink():
            return True
    return False


def scan_blob(name, data, *, source):
    """Never render matching text, decoder errors, URLs or blob contents."""
    if len(data) > MAX_SCAN_BYTES:
        return [{"code": "LARGE_FILE_REQUIRES_REVIEW", "path": name, "source": source}]
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return [{"code": "BINARY_FILE_REQUIRES_REVIEW", "path": name, "source": source}]
    findings = []
    for pattern in (TOKEN, PRIVATE_KEY):
        match = pattern.search(text)
        if match:
            findings.append({"code": "POSSIBLE_LITERAL_CREDENTIAL", "path": name, "source": source,
                             "line": text.count("\n", 0, match.start()) + 1})
    findings.extend(_manifest_findings(name, text, source))
    findings.extend(_template_findings(name, text, source))
    return findings


def scan_index():
    result = subprocess.run(["git", "ls-files", "--stage", "-z", "--", "."], cwd=ROOT,
                            capture_output=True, check=True)
    findings = []
    entries = [entry for entry in result.stdout.split(b"\0") if entry]
    ignored = ignored_paths(entry.split(b"\t", 1)[1].decode("utf-8") for entry in entries)
    for entry in entries:
        header, raw_path = entry.split(b"\t", 1)
        mode, object_id, stage = header.split()
        name = raw_path.decode("utf-8")
        if stage != b"0":
            findings.append({"code": "UNMERGED_INDEX_ENTRY", "path": name, "source": "index"})
            continue
        if mode not in {b"100644", b"100755"}:
            findings.append({"code": "INDEX_ENTRY_REQUIRES_REVIEW", "path": name, "source": "index"})
            continue
        if name in ignored or private_repository_path(name):
            findings.append({"code": "PRIVATE_INDEX_PATH", "path": name, "source": "index"})
            continue
        size = subprocess.run(["git", "cat-file", "-s", object_id.decode("ascii")], cwd=ROOT,
                              capture_output=True, check=True)
        if int(size.stdout) > MAX_SCAN_BYTES:
            findings.append({"code": "LARGE_FILE_REQUIRES_REVIEW", "path": name, "source": "index"})
            continue
        blob = subprocess.run(["git", "cat-file", "blob", object_id.decode("ascii")], cwd=ROOT,
                              capture_output=True, check=True)
        findings.extend(scan_blob(name, blob.stdout, source="index"))
    return findings


def check():
    findings = []
    candidates = git_paths("ls-files", "--cached", "--others", "--exclude-standard")
    tracked_private = git_paths("ls-files", "--cached", "--ignored", "--exclude-standard")
    for path in sorted(tracked_private):
        findings.append({"code": "TRACKED_PRIVATE_FILE", "path": path})
    for path in sorted(set(PROTECTED_PATHS) - ignored_paths(PROTECTED_PATHS)):
        findings.append({"code": "IGNORE_RULE_MISSING", "path": path})
    for path in sorted(ignored_paths(SOURCE_PATHS)):
        findings.append({"code": "REQUIRED_SOURCE_IGNORED", "path": path})
    for name in sorted(candidates):
        path = ROOT / name
        if private_repository_path(name):
            findings.append({"code": "UNAPPROVED_DISTRIBUTION_PATH", "path": name})
            continue
        if has_symlink_component(path):
            findings.append({"code": "SYMLINK_REQUIRES_REVIEW", "path": name})
            continue
        if path.is_file():
            if path.stat().st_size > MAX_SCAN_BYTES:
                findings.append({"code": "LARGE_FILE_REQUIRES_REVIEW", "path": name, "source": "working_tree"})
            else:
                findings.extend(scan_blob(name, path.read_bytes(), source="working_tree"))
    for name in PUBLIC_MARKDOWN:
        path = ROOT / name
        if has_symlink_component(path) or not path.is_file():
            findings.append({"code": "MISSING_DOCUMENT", "path": name})
            continue
        for target in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("https://", "http://", "mailto:", "#")):
                continue
            linked = Path(os.path.abspath(path.parent / unquote(target.split("#", 1)[0])))
            try:
                relative = linked.relative_to(ROOT).as_posix()
            except ValueError:
                findings.append({"code": "DOCUMENT_LINK_OUTSIDE_PROJECT", "path": name})
                continue
            if (private_repository_path(relative) or relative in ignored_paths((relative,)) or
                    has_symlink_component(linked) or not linked.exists()):
                findings.append({"code": "DOCUMENT_LINK_UNAVAILABLE", "path": name, "target": relative})
    findings.extend(scan_index())
    return {"status": "failed" if findings else "ok", "candidate_files": len(candidates),
            "approved_docs": list(PUBLIC_MARKDOWN), "findings": findings}


def main():
    try:
        result = check()
    except (OSError, subprocess.SubprocessError, RuntimeError, UnicodeError, ValueError):
        print(json.dumps({"status": "failed", "code": "GIT_INSPECTION_UNAVAILABLE"}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
