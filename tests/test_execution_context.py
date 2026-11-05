"""Small, offline design smokes for inherited budgets and live execution authority."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from domain.agent_invocation import AgentInvocation
from domain.agent_runtime import AgentRuntimeDescriptor
from domain.capability import CapabilityDependency, CapabilityVersionRecord, ModelPolicyDefinition
from domain.execution_context import ExecutionBudget, ExecutionDeadlineExceeded, effective_timeout
from domain.platform_model import (
    ModelMessage,
    ModelMessages,
    ModelSecret,
    ModelTransportRequest,
    ModelTransportResponse,
)
from domain.task import TaskRecord, TaskStatus
from domain.task_control import ControlState, ExecutionObservation
from domain.task_execution import CompletionOutcome, ExecutionFailure, TaskClaim, TaskLease
from domain.workflow import WorkflowCheckpoint, WorkflowNodeRecord
from orchestration.agent_broker import AgentBroker
from orchestration.agent_model_gateway import PlatformAgentModelGateway
from orchestration.execution_authorization import ExecutionAuthorizer
from orchestration.platform_model_gateway import PlatformModelGateway
from orchestration.platform_worker import PlatformWorker
from orchestration.workflow_executor import WorkflowExecutor


def claim_for(definition=None, owner="admin:ops"):
    now = datetime.now(UTC)
    artifact = CapabilityVersionRecord(
        id="artifact", capability_id="capability", version="1", status="published", content_hash="0" * 64,
        definition=definition or {"nodes": [{"key": "echo", "kind": "rule", "rule": {"operation": "project"}}]},
        created_by="ops", create_time=now, published_by="ops", publish_time=now,
    )
    artifact.content_hash = artifact.fingerprint()
    task = TaskRecord(id="task", project_key="demo", capability_key="flow", capability_kind="workflow",
                      version="1", content_hash=artifact.content_hash, status=TaskStatus.RUNNING, submitted_by=owner,
                      create_time=now, finish_time=None)
    return TaskClaim(task=task, lease=TaskLease(task.id, "attempt", "worker", "a" * 64), input={}, artifact=artifact)


def test_budget_is_absolute_and_descendants_cannot_extend_it(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("domain.execution_context.time.monotonic", lambda: clock[0])
    root = ExecutionBudget.start(10)
    clock[0] = 103.0
    assert root.limit_timeout(60) == 7
    assert root.child(60).deadline == root.deadline
    assert root.child(2).deadline == 105
    clock[0] = 111.0
    with pytest.raises(ExecutionDeadlineExceeded):
        root.limit_timeout(1)
    assert root.child(10).deadline == root.deadline
    assert effective_timeout(None, 4) == 4
    with pytest.raises(ValueError):
        ExecutionBudget.start(True)


def test_child_context_keeps_root_identity_version_and_lease():
    root = claim_for().with_timeout(30)
    node = root.with_timeout(2)
    agent = AgentInvocation(root=node, reference=CapabilityDependency(key="agent", version="1"),
                            artifact=root.artifact, input={}, parent_node_id="node").with_timeout(60)
    assert node.task is root.task and agent.task is root.task
    assert agent.budget is not None and node.budget is not None and root.budget is not None
    assert agent.lease is root.lease and agent.budget.deadline == node.budget.deadline
    assert node.budget.deadline <= root.budget.deadline
    assert root.lease.token not in repr(node)
    assert "budget" not in TaskRecord.model_fields


def test_authorization_uses_live_root_identity_not_agent_identity():
    grants = {"task:submit": {"flow"}, "model:invoke": {"policy"}}
    active = {"enabled": True}
    queries = []

    def resources(project, identity, action):
        queries.append((project, identity, action))
        return grants.get(action, set())

    store = SimpleNamespace(governance=SimpleNamespace(get_project=lambda _: active, execution_resources=resources))
    root = claim_for(owner="service:business")
    agent = AgentInvocation(root, CapabilityDependency(key="agent", version="1"), root.artifact, {}, "node")
    authorizer = ExecutionAuthorizer(store)
    assert authorizer.allowed(agent)
    assert queries[-1] == ("demo", "business", "task:submit")
    assert not authorizer.allowed_resource(agent, "agent:invoke", "agent")
    assert authorizer.allowed_resource(agent, "model:invoke", "policy")
    grants["model:invoke"].clear()
    assert not authorizer.allowed_resource(agent, "model:invoke", "policy")
    active["enabled"] = False
    assert not authorizer.allowed(agent)


def test_administrator_lookup_is_targeted_and_revocation_is_live():
    key = SimpleNamespace(enabled=True, scopes=["admin"])
    names = []

    def get_key(name):
        names.append(name)
        return key

    store = SimpleNamespace(governance=SimpleNamespace(get_project=lambda _: {"enabled": True}),
                            api_key_configs=SimpleNamespace(get=get_key))
    authorizer = ExecutionAuthorizer(store)
    claim = claim_for()
    assert authorizer.allowed(claim)
    key.enabled = False
    assert not authorizer.allowed(claim)
    assert names == ["ops", "ops"]


def test_worker_workflow_adapter_receives_narrowed_budget_without_new_task():
    claim = claim_for({"nodes": [{
        "key": "model", "kind": "llm", "capability": {"key": "policy", "version": "1"},
        "timeout_seconds": 2,
    }], "output_mapping": {"ok": {"source": "node", "node": "model", "pointer": "/ok"}}})
    finished = []
    observed = []

    def begin(lease, node_key, input_hash):
        assert lease is claim.lease
        return WorkflowCheckpoint(WorkflowNodeRecord(
            id="node", task_id=claim.task.id, task_attempt_id=lease.attempt_id, node_key=node_key,
            kind="llm", status="running", input_hash=input_hash, start_time=datetime.now(UTC)), created=True)

    def complete(_lease, **kwargs):
        finished.append(kwargs)
        return CompletionOutcome.COMPLETED

    store = SimpleNamespace(
        governance=SimpleNamespace(get_project=lambda _: {"enabled": True}),
        api_key_configs=SimpleNamespace(get=lambda _: SimpleNamespace(enabled=True, scopes=["admin"])),
        workflows=SimpleNamespace(begin=begin, finish=lambda *_args, **_kwargs: True),
        tasks=SimpleNamespace(reject_disabled_project_tasks=lambda: 0, recover_expired=lambda: 0,
                              claim=lambda *_args, **_kwargs: claim, complete=complete),
        task_control=SimpleNamespace(observe=lambda *_args, **_kwargs: ExecutionObservation(ControlState.ACTIVE)),
    )

    async def adapter(node_claim, _node, _values, _node_id):
        assert node_claim.task is claim.task and node_claim.lease is claim.lease
        observed.append(node_claim.limit_timeout(60))
        return {"ok": True}

    worker = PlatformWorker(store, "worker", {"workflow": WorkflowExecutor(store, {"llm": adapter})})
    assert asyncio.run(worker.run_once())
    assert 0 < observed[0] <= 2
    assert finished == [{"status": "succeeded", "result": {"ok": True}, "error_code": None}]


def test_expired_model_budget_stops_before_credentials_ledger_or_transport(monkeypatch):
    claim = claim_for()
    claim = TaskClaim(claim.task, claim.lease, claim.input, claim.artifact, ExecutionBudget(0))

    class Transport:
        async def complete(self, request: ModelTransportRequest) -> ModelTransportResponse:
            raise AssertionError("An expired execution must not start model IO")

    class Credentials:
        def resolve(self, project_key: str, reference: str) -> ModelSecret:
            raise AssertionError("An expired execution must not resolve credentials")

    gateway = PlatformModelGateway(SimpleNamespace(capabilities=SimpleNamespace()), Transport(), Credentials())
    resolved = SimpleNamespace(artifact=claim.artifact,
                               policy=ModelPolicyDefinition(provider_key="provider", model="model"))
    monkeypatch.setattr(gateway, "resolve_policy", lambda *_args: resolved)
    with pytest.raises(ExecutionFailure) as failure:
        asyncio.run(gateway.complete(claim, "node", CapabilityDependency(key="policy", version="1"),
                                     ModelMessages(messages=[ModelMessage(role="user", content="hello")])))
    assert failure.value.code == "MODEL_EXECUTION_DEADLINE_EXCEEDED"
    assert failure.value.status == "timed_out"


def test_expired_agent_start_does_not_enter_live_resource_resolution():
    claim = claim_for()
    expired = TaskClaim(claim.task, claim.lease, claim.input, claim.artifact, ExecutionBudget(0))
    artifact = claim.artifact.model_copy(update={"definition": {
        "definition_version": "agent.v1", "runtime": "pi",
        "instruction_prompt": {"key": "instructions", "version": "1"},
        "model_policy": {"key": "policy", "version": "1"},
        "limits": {"max_turns": 1, "max_tool_calls": 0, "timeout_seconds": 5},
    }})

    class Gateway(PlatformAgentModelGateway):
        def __init__(self):
            pass

        def resolve_agent(self, claim):
            raise AssertionError("An expired Agent must not enter live resource resolution")

    invocation = AgentInvocation(expired, CapabilityDependency(key="agent", version="1"), artifact, {})
    broker = AgentBroker(invocation, Gateway(), AgentRuntimeDescriptor(key="pi", profile="enterprise-ai.pi.v1"))
    with pytest.raises(ExecutionFailure) as failure:
        asyncio.run(broker.open())
    assert failure.value.code == "AGENT_START_TIMEOUT"
    assert failure.value.status == "timed_out"
