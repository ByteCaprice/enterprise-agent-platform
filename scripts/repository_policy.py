"""Reviewed distribution boundaries. No environment data or Git mutation lives here."""

from fnmatch import fnmatchcase
from pathlib import PurePosixPath

DOCS = (
    "docs/README.md", "docs/overview.md", "docs/api.md", "docs/deployment.md",
    "docs/development.md", "docs/roadmap.md", "docs/repository-policy.md", "docs/frontend-integration.md",
)
PUBLIC_MARKDOWN = ("README.md", "CONTRIBUTING.md", *DOCS)
PUBLIC_NOTICES = ("tests/fixtures/trade_documents/README.md",)
PUBLIC_SCRIPTS = (
    "scripts/backup_platform.py", "scripts/bootstrap_platform_sit.py", "scripts/build_platform_release.py",
    "scripts/check_repository_files.py", "scripts/test_database_config.py", "scripts/repository_policy.py",
)
PUBLIC_TEMPLATES = ("docker/platform/app.env.example", "docker/platform/deployment-policy.example.json")
PUBLIC_STRUCTURED = (
    "framework/agent_runtime/pi/node/package.json", "framework/agent_runtime/pi/node/package-lock.json",
    "docker/platform/compose.yaml", "docker/platform/deployment-policy.example.json",
)
LOCAL_ROOT_FILES = {"rebuild-guide.md", "dev-config.txt", "uv.toml"}
SOURCE_ROOTS = {"domain", "infra", "framework", "orchestration", "interfaces", "tests", "scripts", "docs", "docker"}
SOURCE_ROOT_FILES = {".gitignore", "README.md", "CONTRIBUTING.md", "pyproject.toml", "uv.lock", "MANIFEST.in"}
PRIVATE_PARTS = {
    ".local", ".local-deploy", "private", "secrets", "credentials", ".git", ".hg", ".svn", ".ssh", ".aws", ".azure", ".kube",
    ".gcloud", ".terraform", ".idea", ".vscode", ".claude", ".codex", ".cursor", ".pi",
    ".playwright-mcp", "playwright-report", "test-results",
    "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".pyright", ".cache", "backups", "exports", "screenshots", "recordings",
}
PRIVATE_PATTERNS = (
    ".env*", "*.env", "*.env.*", "*.local", "*.local.*", "dev-config*.txt", ".pypirc", ".npmrc", ".netrc",
    ".gitconfig", ".git-credentials", ".aider*", "agents.md", "claude.md", "gemini.md", "*.iml", "*.code-workspace",
    "pip.conf", "id_rsa", "id_ed25519", "id_ecdsa", "*.pem", "*.key", "*.pub", "*.crt", "*.cer", "*.p12", "*.pfx", "*.jks",
    "*.keystore", "*.kdbx", "*.tfstate", "*.tfstate.*", "*.tfvars", "*.tfvars.*", "*.pyc", "*.pyo",
    "*.log", "*.pid", "*.db", "*.sqlite*", "*.dump", "*.dump.*", "*.backup", "*.bak", "*.sql.gz", "*.sql.zip",
    "*.jsonl", "*.ndjson", "*.har", "*.pcap", "*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.7z", "*.whl",
    "*.pdf", "*.docx", "*.xlsx", "*.csv", "*.png", "*.jpg", "*.jpeg", "*.ipynb",
    "*.npy", "*.npz", "*.parquet", "*.arrow", "*.feather", "*.pkl", "*.pickle", "*.joblib", "*.onnx", "*.safetensors", "*.pt", "*.pth",
)
PROTECTED_PATHS = (
    ".env", ".env_new", ".ENV.production", "nested/.env.sit", "nested/provider.env",
    "config/.env.example", "config/skills.yaml", "config/enterprise-ai-sit/.env.local", "config/private/settings.yaml",
    "dev-config.txt", "dev-config-new.txt", "uv.toml", ".pypirc", ".npmrc", ".netrc",
    "domain/.gitconfig", "framework/.git-credentials", "infra/server.crt", "interfaces/client.cer", "orchestration/id.pub",
    "domain/.claude/settings.json", "framework/.codex/session.py", "infra/.cursor/rules.py", "interfaces/.pi/session.py",
    "tests/.aider.chat.history.md", "domain/AGENTS.md", "infra/CLAUDE.md", "interfaces/GEMINI.md",
    "framework/workspace.iml", "orchestration/project.code-workspace",
    "docs/internal/session-notes.md", "docs/future-work-notes.md", "domain/internal-notes.md", "roadmap.md",
    "rebuild-guide.md", ".local/framework-design.md", "AI-Platform-design.html", "scripts/operator-private.sh",
    "scripts/new-unreviewed-export.py", "unreviewed/seed.sql", "unreviewed/components/sample.py",
    ".idea/misc.xml", ".vscode/settings.json", ".pytest_cache/state", ".playwright-mcp/screenshot.png",
    "dist/release.whl", "build/lib/module.py", "package.egg-info/PKG-INFO", "nested/__pycache__/module.pyc",
    "node_modules/package/index.js", "logs/runtime.log", "backups/snapshot.dump", "snapshot.dump.json",
    "exports/customer.csv", "private/signing.key", "signing.pem", "deployment-policy.json", "mcp-catalog.json",
    "private-settings.yaml", "nested/.ssh/id_ed25519", "nested/credentials/client.ini", "terraform.tfstate",
    "screenshots/page.png", "trace.har", "tests/fixtures/trade_documents/test.pdf", "tests/fixtures/unknown/customer.json",
)
SOURCE_PATHS = (
    ".gitignore", "MANIFEST.in", "pyproject.toml", "uv.lock", "domain/task.py", "interfaces/http/console/icon.svg",
    "infra/_schema_worker.py", "infra/platform_migrations/versions/schema.sql",
    "framework/agent_runtime/pi/node/platform-runner.mjs", "docker/platform/Dockerfile",
    "docker/platform/Dockerfile.dockerignore", *PUBLIC_STRUCTURED, *PUBLIC_TEMPLATES, *PUBLIC_NOTICES,
    *PUBLIC_SCRIPTS, *PUBLIC_MARKDOWN,
)


def sensitive_path(name: str) -> bool:
    """Name-based defense only. It cannot identify arbitrary confidential content."""
    parts = PurePosixPath(name).parts
    lowered = tuple(part.lower() for part in parts)
    if not parts or "\\" in name or name.startswith("/") or ":" in parts[0] or ".." in parts:
        return True
    return any(part in PRIVATE_PARTS for part in lowered) or any(
        fnmatchcase(lowered[-1], pattern) for pattern in PRIVATE_PATTERNS
    )


def private_repository_path(name: str) -> bool:
    if name in {*PUBLIC_MARKDOWN, *PUBLIC_NOTICES, *PUBLIC_STRUCTURED, *PUBLIC_TEMPLATES}:
        return False
    if sensitive_path(name) or name in LOCAL_ROOT_FILES:
        return True
    if PurePosixPath(name).parts[0] not in SOURCE_ROOTS and name not in SOURCE_ROOT_FILES:
        return True
    if name.startswith("config/") or (name.startswith("scripts/") and name not in PUBLIC_SCRIPTS):
        return True
    suffix = PurePosixPath(name).suffix.lower()
    return suffix in {".md", ".rst", ".adoc", ".txt", ".json", ".yaml", ".yml"}


def private_distribution_path(name: str) -> bool:
    """Wheel payload policy is stricter than source-repository policy."""
    if sensitive_path(name):
        return True
    parts = PurePosixPath(name).parts
    if any(part.endswith(".egg-info") for part in parts):
        return True
    if PurePosixPath(name).suffix.lower() in {".md", ".rst", ".json", ".yaml", ".yml", ".toml"} and name not in {
        "framework/agent_runtime/pi/node/package.json", "framework/agent_runtime/pi/node/package-lock.json",
    }:
        # Reviewed distribution license notices are not internal design notes.
        legal_notice = any(part.endswith(".dist-info") for part in parts) and "licenses" in parts and (
            parts[-1].upper().startswith(("LICENSE", "NOTICE", "COPYING"))
        )
        if not legal_notice:
            return True
    return parts[0] not in {"domain", "infra", "framework", "orchestration", "interfaces"} and not parts[0].endswith(".dist-info")
