"""Offline contract checks for explicitly installed capability catalogs."""

import pytest

from domain.agent_runtime import (
    AgentRuntimeCallbacks,
    AgentRuntimeDescriptor,
    AgentRuntimeRegistry,
    AgentRuntimeResult,
    AgentRuntimeStart,
)
from domain.platform_tool import InstalledReadonlyTool
from framework.readonly_tools import ReadonlyToolRegistry


async def handler(_context, values):
    return values


def test_readonly_catalog_copies_input_and_rejects_normal_reassignment():
    installed = InstalledReadonlyTool("test.lookup", "1", handler)
    source = [installed]
    catalog = ReadonlyToolRegistry(source)
    source.clear()

    assert catalog.resolve("test.lookup", "1") is installed
    assert catalog.resolve("test.lookup", "2") is None
    with pytest.raises(AttributeError, match="immutable"):
        catalog._handlers = {}


def test_readonly_catalog_rejects_duplicate_key_and_version():
    installed = InstalledReadonlyTool("test.lookup", "1", handler)
    with pytest.raises(ValueError, match="unique"):
        ReadonlyToolRegistry([installed, installed])


def test_agent_runtime_catalog_resolves_only_installed_key_and_rejects_duplicates():
    class Runtime:
        descriptor = AgentRuntimeDescriptor(key="test-runtime", profile="test.runtime.v1")

        async def check_ready(self) -> None:
            return None

        async def run(self, start: AgentRuntimeStart, callbacks: AgentRuntimeCallbacks) -> AgentRuntimeResult:
            raise NotImplementedError

    runtime = Runtime()
    source = [runtime]
    catalog = AgentRuntimeRegistry(source)
    source.clear()

    installed = catalog.get("test-runtime")
    assert installed is not None and installed.adapter is runtime
    assert catalog.get("missing-runtime") is None
    with pytest.raises((AttributeError, TypeError)):
        type(catalog).__setattr__(catalog, "_runtimes", {})
    with pytest.raises(ValueError, match="Duplicate"):
        AgentRuntimeRegistry([runtime, runtime])
