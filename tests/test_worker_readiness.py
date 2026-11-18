"""Offline tests for bounded, read-only Worker coverage of pinned tasks."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.dialects import postgresql
from test_business_code_contracts import (
    FLOW_REF,
    TOOL_REF,
    code_definition,
    code_tool,
    inspect,
    installed,
    pin,
    published,
)

from domain.execution_requirements import WORKFLOW_ADAPTER_PROFILES
from domain.workflow_runtime import WorkflowAdapterDescriptor
from infra.store.repository import worker_readiness as readiness
from infra.store.repository.worker_health import _public_worker
from interfaces.worker import platform


def queued(root, task_id="oldest", **overrides):
    return SimpleNamespace(**({"id": task_id, "project_key": "payments", "capability_id": root.capability_id,
                              "version": root.artifact.version, "content_hash": root.artifact.content_hash} | overrides))


def observed(capabilities, *, role="task"):
    return {"role": role, "executors": readiness.heartbeat_executors(sorted(capabilities.executor_kinds), capabilities)}


class ReadSession:
    """Execute nothing; provide stored pins while checking the generated read queries."""

    def __init__(self, tasks, artifacts):
        self.tasks = tasks
        self.artifacts = artifacts
        self.lookups = 0

    def execute(self, query):
        compiled = query.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert "FOR UPDATE" not in sql and "FOR SHARE" not in sql
        assert "payments" in compiled.params.values()
        if "FROM ai_task" in sql:
            assert "ai_task.status =" in sql and "queued" in compiled.params.values()
            assert "ORDER BY ai_task.create_time, ai_task.id" in sql
            assert "LIMIT" in sql and readiness.QUEUE_SCAN_LIMIT + 1 in compiled.params.values()
            assert "ai_task.input" not in sql
            return SimpleNamespace(all=lambda: self.tasks[:readiness.QUEUE_SCAN_LIMIT + 1])
        self.lookups += 1
        key_column = "id" if "ai_capability.id = %(" in sql else "key"
        key = next(value for name, value in compiled.params.items() if name.startswith(f"{key_column}_"))
        artifact = self.artifacts.get((key, compiled.params["version_1"]))
        return SimpleNamespace(one_or_none=lambda: None if artifact is None else (
            SimpleNamespace(id=artifact.capability_id, key=artifact.key, kind=artifact.kind),
            SimpleNamespace(to_domain=lambda: artifact.artifact),
        ))


def check(session, workers=(), *, truncated=False):
    return readiness.queued_readiness(session, "payments", enabled=True, workers=workers, workers_truncated=truncated)


def test_full_pinned_closure_must_be_covered_by_one_worker_with_exact_roles_and_digest():
    root = published("flow", "workflow", code_definition([
        {"key": "lookup", "kind": "tool", "capability": pin("catalog")},
    ]), dependencies=[pin("catalog")])
    tool = published("catalog", "tool", code_tool())
    requirements = inspect(root, [tool])
    artifacts = {(art.key, art.artifact.version): art for art in (root, tool)}
    adapter = WorkflowAdapterDescriptor(kind="tool", profile=WORKFLOW_ADAPTER_PROFILES["tool"])
    wrong_adapter = WorkflowAdapterDescriptor(kind="tool", profile=adapter.profile + ".different")
    wrong_source = TOOL_REF.model_copy(update={"digest": "c" * 64})
    for workers, compatible in [
        ([installed(workflow_implementations=[FLOW_REF], tool_implementations=[TOOL_REF], adapters=[wrong_adapter])], False),
        ([installed(workflow_implementations=[FLOW_REF], tool_implementations=[wrong_source], adapters=[adapter])], False),
        ([installed(workflow_implementations=[TOOL_REF], tool_implementations=[FLOW_REF], adapters=[adapter])], False),
        ([installed(workflow_implementations=[FLOW_REF], adapters=[adapter]),
          installed(tool_implementations=[TOOL_REF], adapters=[adapter])], False),
        ([installed(profiles=(), workflow_implementations=[FLOW_REF], tool_implementations=[TOOL_REF], adapters=[adapter])], False),
        ([installed(workflow_implementations=[FLOW_REF], tool_implementations=[TOOL_REF], adapters=[adapter])], True),
    ]:
        session = ReadSession([queued(root), queued(root, "duplicate-pin")], artifacts)
        rows = [observed(worker) for worker in workers]
        assert any(worker.supports(requirements) for worker in workers) is compatible
        result = check(session, rows)
        assert result["compatible"] == (2 if compatible else 0)
        assert {row["reason"] for row in result["issues"]} == (set() if compatible else {"no_compatible_worker"})
        assert session.lookups == 2  # Identical pins are inspected once per response.
        assert len(result["issues"]) == (0 if compatible else 2)
        assert [row["task_id"] for row in result["issues"]] == ([] if compatible else ["oldest", "duplicate-pin"])
    assert _public_worker(rows[0])["executors"] == ["workflow"]
    assert TOOL_REF.key not in json.dumps(rows)
    assert TOOL_REF.digest not in json.dumps(rows)


@pytest.mark.parametrize("problem", ["legacy", "unrelated", "malformed", "oversized", "truncated", "callback", "hash"])
def test_unknown_and_unavailable_are_distinct_from_no_compatible_worker(problem):
    root = published("flow", "workflow", code_definition())
    matching = installed(workflow_implementations=[FLOW_REF])
    workers = []
    args = {}
    if problem == "legacy":
        workers.append({"role": "task", "executors": ["workflow"]})
    elif problem == "unrelated":
        workers.append({"role": "task", "executors": ["agent"]})
    elif problem == "malformed":
        workers.append({"role": "task", "executors": ["workflow", {"execution_manifest_v1": ["bad"]}]})
    elif problem == "oversized":
        huge = installed(workflow_implementations=[
            FLOW_REF.model_copy(update={"key": f"flow-{i}"}) for i in range(readiness._MANIFEST_ITEM_LIMIT)
        ])
        assert observed(huge)["executors"] == ["workflow"]
        workers.append(observed(huge))
    elif problem == "callback":
        workers.append(observed(matching, role="callback"))
    elif problem == "hash":
        args["content_hash"] = "0" * 64
    task = queued(root, **args)
    session = ReadSession([task], {("flow", "1"): root})
    result = check(session, workers, truncated=problem == "truncated")
    reasons = {"callback": "no_compatible_worker", "unrelated": "no_compatible_worker", "hash": "requirements_unavailable"}
    assert result["issues"] == [{"task_id": task.id, "reason": reasons.get(problem, "worker_coverage_unknown")}]
    if problem != "hash":
        assert check(session, [*workers, observed(matching)], truncated=True)["compatible"] == 1


def test_fifo_and_artifact_lookup_caps_and_disabled_project(monkeypatch):
    root = published("flow", "workflow", code_definition())
    tasks = [queued(root, f"task-{i}", capability_id=f"flow-{i}") for i in range(4)]
    session = ReadSession(tasks, {})
    monkeypatch.setattr(readiness, "QUEUE_SCAN_LIMIT", 2)
    monkeypatch.setattr(readiness, "ARTIFACT_LOOKUP_LIMIT", 1)
    assert check(session) == {
        "scan_limit": 2, "scanned": 2, "queue_truncated": True, "artifact_lookup_limit_reached": True,
        "compatible": 0, "issues": [
            {"task_id": "task-0", "reason": "requirements_unavailable"},
            {"task_id": "task-1", "reason": "inspection_limit"},
        ],
    }
    assert session.lookups == 1
    assert readiness.queued_readiness(object(), "payments", enabled=False, workers=[], workers_truncated=False)["scanned"] == 0


def test_worker_start_advertises_same_manifest_used_for_claim(monkeypatch):
    start = Mock(return_value="instance")
    health = SimpleNamespace(start=start, heartbeat=Mock())
    store = SimpleNamespace(worker_health=health, **{key: object() for key in (
        "capabilities", "governance", "api_key_configs", "models", "tool_bindings", "agents",
        "workflows", "task_queue", "task_control", "task_completion",
    )})
    worker = SimpleNamespace(worker_id="worker")

    async def run_once():
        assert start.call_count == 1
        assert start.call_args.kwargs["capabilities"] is factory.call_args.kwargs["capabilities"]
        return True

    worker.run_once = run_once
    factory = Mock(return_value=worker)
    monkeypatch.setattr(platform, "PlatformWorker", factory)
    monkeypatch.setattr(platform, "check_validation_backend", Mock())
    assert asyncio.run(platform.run_workflow_worker(store, once=True))
    health.heartbeat.assert_called_once_with("instance", "stopped")
