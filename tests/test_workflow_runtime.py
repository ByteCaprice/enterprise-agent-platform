"""Small offline smokes for adapter installation and static plan inspection."""

from datetime import UTC, datetime
from operator import setitem
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domain.capability import CapabilityDependency, CapabilityVersionRecord
from domain.execution_requirements import (
    WORKFLOW_CHECKPOINT_PROFILE,
    WORKFLOW_INTERPRETER_PROFILE,
    InstalledExecutionCapabilities,
)
from domain.task_worker import WorkerServices
from domain.workflow_runtime import WorkflowAdapterDescriptor, WorkflowNodeRegistry
from infra.rate_limiter import RateLimiter
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


def test_worker_manifest_must_match_installed_workflow_catalog():
    workflow = WorkflowExecutor(SimpleNamespace(), SimpleNamespace(), {"agent": handler})
    services = WorkerServices(queue=None, control=None, completion=None, authorization=None, validation=None)
    with pytest.raises(ValueError, match="manifest"):
        PlatformWorker(services, "worker", {"workflow": workflow})
    capabilities = InstalledExecutionCapabilities.from_descriptors(
        ("workflow",), workflow_interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
        workflow_checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE, workflow_adapters=workflow.adapter_descriptors,
    )
    worker = PlatformWorker(services, "worker", {"workflow": workflow}, capabilities=capabilities)
    assert worker.capabilities is capabilities
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
    app.state.rate_limiter = RateLimiter()
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
