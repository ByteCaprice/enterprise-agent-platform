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
    "domain": {"infra", "framework", "orchestration", "interfaces"},
    "infra": {"framework", "orchestration", "interfaces"},
    "framework": {"infra", "orchestration", "interfaces"},
    "orchestration": {"interfaces"},
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
    )
    for name in services:
        source = (REPO_ROOT / "orchestration" / f"{name}.py").read_text(encoding="utf-8")
        dependencies = {node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)}
        assert "orchestration.platform_worker" not in dependencies, name
        assert "orchestration.capability_service" not in dependencies, name
