"""Architectural guard: the layer dependency graph must stay acyclic.

Interfaces compose application use cases. Framework algorithms and infrastructure
adapters are siblings, each depending on domain contracts, never on one another.

Cycles between top-level packages are the objective smell this guards against
— they prevent reasoning about / testing any module in isolation.  If you need
to add an import this test forbids, the dependency is pointing the wrong way:
move the shared code *down* to a lower layer instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Each layer maps to the set of sibling layers it must never import.
FORBIDDEN: dict[str, set[str]] = {
    "domain": {"infra", "framework", "orchestration", "interfaces", "capability_hub"},
    "infra": {"framework", "orchestration", "interfaces", "capability_hub"},
    "framework": {"infra", "orchestration", "interfaces", "capability_hub"},
    "orchestration": {"interfaces", "capability_hub"},
    "capability_hub": {"infra", "interfaces"},
}


def _imported_roots(source: str) -> set[str]:
    """Return the top-level package name of every import in *source*."""
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:  # absolute import only
                roots.add(node.module.split(".")[0])
    return roots


def _layer_files(layer: str) -> list[Path]:
    return [p for p in (REPO_ROOT / layer).rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_has_no_upward_imports(layer: str) -> None:
    forbidden = FORBIDDEN[layer]
    violations: list[str] = []
    for path in _layer_files(layer):
        roots = _imported_roots(path.read_text(encoding="utf-8"))
        for bad in sorted(roots & forbidden):
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"{rel} imports `{bad}` (forbidden for layer `{layer}`)")
    assert not violations, "Upward layer dependency detected:\n" + "\n".join(violations)


def test_execution_services_do_not_depend_on_worker_scheduling():
    services = (
        "execution_authorization", "workflow_executor", "workflow_llm", "workflow_agent",
        "agent_executor", "agent_broker", "agent_model_gateway", "platform_model_gateway", "platform_tool_gateway",
        "model_policy_resolver", "code_workflow",
    )
    for name in services:
        source = (REPO_ROOT / "orchestration" / f"{name}.py").read_text(encoding="utf-8")
        dependencies = {node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)}
        assert "orchestration.platform_worker" not in dependencies, name
        assert "orchestration.capability_service" not in dependencies, name
        assert not _imported_roots(source) & {"infra", "interfaces"}, name
        assert not any(isinstance(node, ast.Attribute) and node.attr == "store"
                       for node in ast.walk(ast.parse(source))), f"{name} must use execution-only ports"


def test_worker_depends_on_execution_ports_not_platform_composition():
    source = (REPO_ROOT / "orchestration/platform_worker.py").read_text(encoding="utf-8")
    assert not _imported_roots(source) & {"infra", "interfaces"}
    tree = ast.parse(source)
    dependencies = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "domain.task_worker" in dependencies
    assert "orchestration.execution_authorization" not in dependencies
    assert not any(
        isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "self" and node.attr == "store"
        for node in ast.walk(tree)
    ), "Worker must receive narrow services, not retain a PlatformStore"


def test_task_completion_and_inbox_have_distinct_persistence_ownership():
    tree = ast.parse((REPO_ROOT / "infra/store/repository/task.py").read_text(encoding="utf-8"))
    inbox = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TaskRepository")
    assert not any(isinstance(node, ast.FunctionDef) and node.name == "complete" for node in inbox.body)
    source = (REPO_ROOT / "domain/task_completion.py").read_text(encoding="utf-8")
    assert not _imported_roots(source) & {"infra", "orchestration", "interfaces", "framework"}


def test_journal_append_does_not_own_budget_or_delivery_and_agents_do_not_import_repositories():
    journal = ast.parse((REPO_ROOT / "infra/store/repository/task_events.py").read_text(encoding="utf-8"))
    dependencies = {node.module for node in ast.walk(journal) if isinstance(node, ast.ImportFrom)}
    assert "infra.store.repository.callbacks" not in dependencies
    assert "infra.store.repository.limits" not in dependencies
    agents = ast.parse((REPO_ROOT / "infra/store/repository/agents.py").read_text(encoding="utf-8"))
    dependencies = {node.module for node in ast.walk(agents) if isinstance(node, ast.ImportFrom)}
    assert "infra.store.repository.models" not in dependencies
    assert "infra.store.repository.tools" not in dependencies


def test_process_runtime_has_no_http_dependency():
    source = (REPO_ROOT / "interfaces/runtime.py").read_text(encoding="utf-8")
    assert not _imported_roots(source) & {"fastapi", "starlette", "uvicorn"}
    tree = ast.parse(source)
    dependencies = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(module and module.startswith("interfaces.http") for module in dependencies)
