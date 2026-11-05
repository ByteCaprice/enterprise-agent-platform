"""Pure read-only tool contracts and explicit runtime registration boundaries."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from domain.platform_tool import InstalledReadonlyTool, ToolContext, ToolDefinition
from framework.readonly_tools import ReadonlyToolRegistry, builtin_readonly_tools


@pytest.mark.parametrize("extra", [
    {"endpoint": {"protocol": "python", "target": "os:system"}}, {"command": "sh"},
    {"url": "http://127.0.0.1"}, {"operation_type": "side_effecting"}, {"operation_type": "idempotent"},
])
def test_tool_definition_cannot_install_code_network_or_side_effects(extra):
    with pytest.raises(ValidationError):
        ToolDefinition.model_validate({"binding_key": "approved", "operation_type": "read_only", **extra})


def test_registration_requires_async_reviewed_code_and_unique_versions():
    def synchronous(_scope, _values):
        return {}

    with pytest.raises(ValueError):
        InstalledReadonlyTool("unsafe", "1", synchronous)
    handler = builtin_readonly_tools().resolve("json.project", "1")
    assert handler is not None
    with pytest.raises(ValueError):
        ReadonlyToolRegistry([handler, handler])
    assert builtin_readonly_tools().resolve("os.system", "1") is None
    assert builtin_readonly_tools().resolve("json.project", "uninstalled") is None


def test_builtin_json_projection_is_real_pure_work_without_shared_mutation():
    handler = builtin_readonly_tools().resolve("json.project", "1")
    assert handler is not None
    values = {"data": {"visible": {"amount": 2}, "private": "not selected"}, "fields": ["visible"]}
    scope = ToolContext("project", "service:identity", "task", "attempt", "node", "tool", "1")
    result = asyncio.run(handler.execute(scope, values))
    assert result == {"visible": {"amount": 2}}
    result["visible"]["amount"] = 3
    assert values["data"]["visible"]["amount"] == 2


def test_worker_import_does_not_load_optional_sdk():
    code = """
import sys
sys.path.insert(0, sys.argv[1])
from interfaces.worker.platform import run_workflow_worker
from infra.platform_store import PlatformStore
assert 'mcp' not in sys.modules
assert 'framework.agent_runtime.pi.adapter' not in sys.modules
"""
    subprocess.run([sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1])], check=True,
                   capture_output=True, text=True)
