"""Small offline smokes for adapter installation and static plan inspection."""

from datetime import UTC, datetime
from operator import setitem
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domain.capability import CapabilityDependency, CapabilityVersionRecord
from domain.workflow_runtime import WorkflowAdapterDescriptor, WorkflowNodeRegistry
from interfaces.http.routes.capabilities import router
from orchestration.control_plane_auth import ControlPlanePrincipal
from orchestration.platform_worker import PlatformWorker
from orchestration.workflow_executor import WorkflowExecutor


async def handler(_claim, _node, _values, _node_id):
    return {}


def test_adapter_installation_is_explicit_serial_and_read_only():
    registry = WorkflowNodeRegistry.from_mapping({"llm": handler})
    installed = registry.get("llm")
    assert installed is not None and installed.descriptor.execution == "serial"
    assert installed.descriptor.retry == "unsupported"
    with pytest.raises(TypeError):
        setitem(cast(Any, registry.executors), "tool", handler)
    with pytest.raises(AttributeError):
        cast(Any, registry)._installed = {}
    with pytest.raises(ValueError):
        WorkflowNodeRegistry.from_mapping({"rule": handler})

    class Tool:
        descriptor = WorkflowAdapterDescriptor(kind="tool", profile="test.tool.v1")

        async def __call__(self, claim, node, values, node_id):
            return {}

    with pytest.raises(ValueError, match="kind"):
        WorkflowNodeRegistry.from_mapping({"llm": Tool()})
    with pytest.raises(ValueError, match="Duplicate"):
        WorkflowNodeRegistry([Tool(), Tool()])


def test_worker_agent_role_must_match_standard_workflow_catalog():
    workflow = WorkflowExecutor(SimpleNamespace(), {"agent": handler})
    with pytest.raises(ValueError, match="role"):
        PlatformWorker(SimpleNamespace(), "worker", {"workflow": workflow})
    worker = PlatformWorker(SimpleNamespace(), "worker", {"workflow": workflow}, workflow_agents=True)
    assert worker.workflow_agents
    with pytest.raises(TypeError):
        setitem(cast(Any, worker.executors), "agent", handler)


def test_plan_endpoint_is_authenticated_metadata_only_and_does_not_execute():
    now = datetime.now(UTC)
    artifact = CapabilityVersionRecord(
        id="artifact", capability_id="flow", version="1", status="published", content_hash="0" * 64,
        created_by="ops", create_time=now, published_by="ops", publish_time=now,
        definition={"nodes": [
            {"key": "first", "kind": "rule", "rule": {"operation": "project"},
             "input_mapping": {"value": {"source": "literal", "value": "private-literal"}}},
            {"key": "model", "kind": "llm", "capability": {"key": "policy", "version": "1"},
             "depends_on": ["first"]},
        ]}, dependencies=[CapabilityDependency(key="policy", version="1")],
    )
    artifact.content_hash = artifact.fingerprint()
    repository = SimpleNamespace(get=lambda *_: {"id": "flow", "project_key": "demo", "key": "flow", "kind": "workflow"},
                                 get_version=lambda *_: artifact)
    principal = ControlPlanePrincipal("ops", frozenset({"admin"}))
    app = FastAPI()
    app.state.store = SimpleNamespace(capabilities=repository)
    app.state.control_plane_auth = SimpleNamespace(
        authenticate=lambda token: principal if token == "synthetic-key" else None,
        allowed_resources=lambda *_: {"*"},
    )
    app.state.rate_limiter = SimpleNamespace(settings={"enabled": False})
    app.include_router(router)
    path = "/v2/projects/demo/capabilities/flow/versions/1/plan"
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        response = client.get(path, headers={"x-api-key": "synthetic-key"})
    assert response.status_code == 200
    plan = response.json()
    assert plan["required_adapters"] == ["llm"] and plan["requires_runtime_validation"]
    assert plan["content_hash"] == artifact.content_hash
    assert [node["key"] for node in plan["nodes"]] == ["first", "model"]
    assert "private-literal" not in response.text and "input_mapping" not in response.text
