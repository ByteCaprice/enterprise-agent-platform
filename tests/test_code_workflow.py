"""Small offline contracts for generic code Workflow execution and ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from domain.business_code import (
    CODE_WORKFLOW_CHECKPOINT_PROFILE,
    CODE_WORKFLOW_INTERPRETER_PROFILE,
    CODE_WORKFLOW_RESULT_KEY,
    ActivityCall,
    CodeReference,
)
from domain.capability import CapabilityVersionRecord
from domain.execution_ports import ExecutionResources
from domain.execution_requirements import WORKFLOW_ADAPTER_PROFILES
from domain.schema_validation import JSONValidationError, JSONValidationUnavailable
from domain.task import TaskRecord, canonical_json
from domain.task_completion import (
    CompletionCommand,
    CompletionEvidence,
    CompletionTarget,
    NodeCompletionEvidence,
    require_completion_evidence,
)
from domain.task_execution import (
    ExecutionFailure,
    ExecutionResourceUnstoppable,
    TaskCancellationRequested,
    TaskClaim,
    TaskLease,
    TaskLeaseLost,
)
from domain.workflow import WorkflowCheckpoint, WorkflowNodeRecord
from domain.workflow_runtime import WorkflowAdapterDescriptor, WorkflowNodeRegistry
from framework.business_code import (
    BusinessStepContext,
    CodeWorkflowRegistry,
    CodeWorkflowRegistryError,
    InstalledCodeWorkflow,
    SourceManifest,
)
from orchestration.code_workflow import CodeWorkflowExecutor

REFERENCE = CodeReference(key="reviewed-flow", version="1", digest="a" * 64)


def detached(value):
    return json.loads(canonical_json(value))


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


async def identity(_ctx, values):
    return values


async def call_identity(ctx, values):
    return (await ctx.call("calculate", values)).require_value()


def artifact(activities, *, reference=REFERENCE, max_parallelism=4, output_schema=None, identity="flow"):
    dependencies = {tuple(sorted(item["capability"].items())) for item in activities if "capability" in item}
    dependencies.update(tuple(sorted(item["prompt"].items())) for item in activities if "prompt" in item)
    now = datetime.now(UTC)
    result = CapabilityVersionRecord(
        id="artifact-" + identity, capability_id=identity, version="1", status="published", content_hash="0" * 64,
        created_by="ops", create_time=now, published_by="ops", publish_time=now,
        definition={"definition_version": "workflow.code.v1", "implementation": reference.model_dump(),
                    "activities": activities, "max_parallelism": max_parallelism},
        dependencies=[dict(item) for item in dependencies], output_schema=output_schema or {"type": "object"},
    )
    result.content_hash = result.fingerprint()
    return result


class Checkpoints:
    def __init__(self, claim):
        self.claim = claim
        self.artifacts = {"flow": claim.artifact}
        self.rows = {}
        self.outputs = {}
        self.calls = []
        self.live = True
        self.on_finish = None

    def begin(self, lease, node_key, input_hash, *, interpreter_profile, checkpoint_profile, scope, parent_node_id):
        assert lease is self.claim.lease
        assert interpreter_profile == CODE_WORKFLOW_INTERPRETER_PROFILE
        assert checkpoint_profile == CODE_WORKFLOW_CHECKPOINT_PROFILE
        assert scope.content_hash == self.artifacts[scope.workflow_key].content_hash
        self.calls.append(("begin", node_key))
        if not self.live:
            return None
        if node_key in self.rows:
            return WorkflowCheckpoint(self.rows[node_key], self.outputs.get(node_key), created=False)
        kind = "result" if scope.activity_key is None else next(
            item["kind"] for item in self.artifacts[scope.workflow_key].definition["activities"]
            if item["key"] == scope.activity_key)
        row = WorkflowNodeRecord(
            id=f"receipt-{node_key}", task_id=lease.task_id, task_attempt_id=lease.attempt_id, node_key=node_key,
            kind=kind, status="running", input_hash=input_hash, start_time=datetime.now(UTC),
            scope=scope, parent_node_id=parent_node_id,
        )
        self.rows[node_key] = row
        return WorkflowCheckpoint(row, created=True)

    def finish(self, lease, node_id, *, status, output=None, error_code=None):
        assert lease is self.claim.lease
        key = next(key for key, row in self.rows.items() if row.id == node_id)
        if self.on_finish is not None:
            self.on_finish(key)
        if not self.live:
            return False
        row = self.rows[key]
        assert row.status == "running", "receipts must settle exactly once"
        self.calls.append(("finish", key, status))
        row.status, row.error_code = status, error_code
        row.finish_time = datetime.now(UTC)
        row.output_hash = digest(output) if output is not None else None
        self.outputs[key] = detached(output)
        return True

    def activity_rows(self, key):
        return [row for row in self.rows.values() if row.scope.activity_key == key]


class Authorization:
    def __init__(self):
        self.enabled = True
        self.checked = []
        self.resources = []

    def allowed(self, claim):
        self.checked.append(claim)
        return self.enabled

    def allowed_resource(self, claim, action, key):
        self.resources.append((claim, action, key))
        return self.enabled


class Validation:
    def __init__(self):
        self.calls = []
        self.error = None

    def validate(self, instance, schema):
        self.calls.append((detached(instance), detached(schema)))
        if self.error is not None:
            raise self.error
        if set(schema.get("required", ())) - instance.keys():
            raise JSONValidationError("INSTANCE_INVALID")


class Adapter:
    def __init__(self, kind="tool", error=None):
        self.descriptor = WorkflowAdapterDescriptor(kind=kind, profile=WORKFLOW_ADAPTER_PROFILES[kind])
        self.calls = []
        self.error = error

    async def __call__(self, claim, node, values, node_id):
        assert node.input_mapping == {} and node.depends_on == [] and node.when is None
        assert claim.budget is not None
        self.calls.append((claim, node, detached(values), node_id))
        if self.error is not None:
            raise self.error
        return values


def harness(run=call_identity, *, steps=None, activities=None, values=None, reference=REFERENCE,
            adapters=(), output_schema=None, max_parallelism=4):
    published = artifact(activities or [{"key": "calculate", "kind": "step"}], reference=reference,
                         output_schema=output_schema, max_parallelism=max_parallelism)
    task = TaskRecord(id="task", project_key="project", capability_key="flow", capability_kind="workflow", version="1",
                      content_hash=published.content_hash, submitted_by="service:submitter", status="running",
                      create_time=datetime.now(UTC), finish_time=None)
    claim = TaskClaim(task, TaskLease(task.id, "attempt", "worker", "never-exposed"),
                      values if values is not None else {"amount": 12}, published)
    checkpoints = Checkpoints(claim)
    authorization, validation = Authorization(), Validation()
    registry = CodeWorkflowRegistry([InstalledCodeWorkflow(REFERENCE, run, {"calculate": identity} if steps is None else steps)])
    resources = ExecutionResources(None, authorization, validation)
    executor = CodeWorkflowExecutor(checkpoints, resources, registry, WorkflowNodeRegistry(adapters))
    return SimpleNamespace(executor=executor, claim=claim, checkpoints=checkpoints, authorization=authorization,
                           validation=validation, registry=registry)


def execute(env):
    return asyncio.run(env.executor(env.claim))


def assert_failure(env, code, *, status="failed"):
    with pytest.raises(ExecutionFailure) as caught:
        execute(env)
    assert caught.value.code == code and caught.value.status == status
    if CODE_WORKFLOW_RESULT_KEY in env.checkpoints.rows:
        result = env.checkpoints.rows[CODE_WORKFLOW_RESULT_KEY]
        assert result.status == "failed" and result.error_code == code
    return caught.value


def test_declared_calls_detach_values_and_use_hashed_scoped_evidence():
    metadata, retained = [], []

    async def calculate(ctx, values):
        assert isinstance(ctx, BusinessStepContext)
        assert not any(hasattr(ctx, name) for name in ("claim", "lease", "store", "gateway", "parallel"))
        metadata.append(ctx)
        output = {"total": values["amount"] * 2}
        retained.append(output)
        return output

    async def run(ctx, values):
        assert not any(hasattr(ctx, name) for name in ("claim", "lease", "step", "tool", "agent", "llm"))
        call = ctx.plan("calculate", values, key="private invocation label")
        values["amount"] = -1
        call.values()["amount"] = -2
        subtotal = (await ctx.parallel({"subtotal": call}))["subtotal"].require_value()
        subtotal["local"] = True
        return (await ctx.call("review", subtotal)).require_value()

    adapter = Adapter("agent")
    env = harness(run, steps={"calculate": calculate}, activities=[
        {"key": "calculate", "kind": "step"},
        {"key": "review", "kind": "agent", "capability": {"key": "reviewer", "version": "3"}},
    ], adapters=[adapter])
    assert execute(env) == {"total": 24, "local": True}
    row = env.checkpoints.activity_rows("calculate")[0]
    key_hash = hashlib.sha256(b"private invocation label").hexdigest()
    frame_id = env.checkpoints.rows[CODE_WORKFLOW_RESULT_KEY].id
    assert row.scope.invocation_hash == key_hash and row.parent_node_id == frame_id
    assert row.node_key == "call." + hashlib.sha256((frame_id + "\0calculate\0" + key_hash).encode()).hexdigest()
    assert "private invocation label" not in repr(env.checkpoints.rows)
    assert metadata[0].node_id == row.id
    assert adapter.calls[0][1].key == "review" and adapter.calls[0][3] == env.checkpoints.activity_rows("review")[0].id
    retained[0]["total"] = 0
    assert env.checkpoints.outputs[row.node_key] == {"total": 24}
    assert env.claim.input == {"amount": 12}


@pytest.mark.parametrize("explicit", [True, False])
def test_optional_branches_have_no_synthetic_calls(explicit):
    async def run(ctx, values):
        if explicit:
            assert (await ctx.skip("unselected")).status == "skipped"
        return (await ctx.call("calculate", values)).require_value()

    env = harness(run, steps={"calculate": identity, "unselected": identity}, activities=[
        {"key": "calculate", "kind": "step"}, {"key": "unselected", "kind": "step", "required": False},
    ])
    assert execute(env) == {"amount": 12}
    rows = env.checkpoints.activity_rows("unselected")
    assert len(rows) == int(explicit)
    if rows:
        assert rows[0].status == "skipped" and rows[0].output_hash is None


@pytest.mark.parametrize("case,code", [
    ("unknown", "CODE_WORKFLOW_ACTIVITY_UNKNOWN"), ("duplicate", "CODE_WORKFLOW_CALL_ALREADY_USED"),
    ("skip", "CODE_WORKFLOW_REQUIRED_ACTIVITY_SKIPPED"), ("missing", "CODE_WORKFLOW_REQUIRED_ACTIVITY_MISSING"),
    ("incomplete", "CODE_WORKFLOW_CALL_INCOMPLETE"), ("concurrent", "CODE_WORKFLOW_CONCURRENT_CALL"),
    ("key", "CODE_WORKFLOW_CALL_INVALID"),
])
def test_caught_sdk_errors_and_incomplete_requests_poison_frame(case, code):
    async def run(ctx, values):
        try:
            if case == "unknown":
                await ctx.call("unknown", values)
            elif case == "duplicate":
                await ctx.call("calculate", values)
                await ctx.call("calculate", values)
            elif case == "skip":
                await ctx.skip("calculate")
            elif case == "incomplete":
                ctx.call("calculate", values)
            elif case == "concurrent":
                ctx.call("calculate", values)
                ctx.call("calculate", values)
            elif case == "key":
                await ctx.call("calculate", values, key="bad\nkey")
        except ExecutionFailure:
            pass
        return {}

    assert_failure(harness(run), code)


@pytest.mark.parametrize("case", ["unknown", "duplicate", "limit", "concurrency", "size"])
def test_groups_validate_every_call_before_io(case):
    async def run(ctx, values):
        calls = {"first": ctx.plan("calculate", values, key="1")}
        concurrency = None
        if case == "unknown":
            calls["last"] = ActivityCall("unknown", {})
        elif case == "duplicate":
            calls["last"] = calls["first"]
        elif case == "limit":
            calls["last"] = ctx.plan("calculate", {}, key="2")
        elif case == "concurrency":
            concurrency = 5
        elif case == "size":
            calls = {str(index): ActivityCall("calculate", {}, key=str(index)) for index in range(1001)}
        await ctx.parallel(calls, concurrency=concurrency)
        return {}

    env = harness(run)
    with pytest.raises(ExecutionFailure):
        execute(env)
    assert list(env.checkpoints.rows) == [CODE_WORKFLOW_RESULT_KEY]


def test_map_is_bounded_and_returns_input_order_with_stable_index_identities():
    active, maximum = 0, 0

    async def step(_ctx, values):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        await asyncio.sleep(0.005 if values["index"] == 0 else 0)
        active -= 1
        return values

    async def run(ctx, _values):
        results = await ctx.map("calculate", [{"index": index} for index in range(6)], concurrency=2)
        return {"indices": [result.require_value()["index"] for result in results]}

    env = harness(run, steps={"calculate": step}, activities=[{"key": "calculate", "kind": "step", "max_calls": 6}])
    assert execute(env) == {"indices": list(range(6))}
    assert maximum == 2 and active == 0
    assert {row.scope.invocation_hash for row in env.checkpoints.activity_rows("calculate")} == {
        hashlib.sha256(str(index).encode()).hexdigest() for index in range(6)}


def test_raw_gather_of_public_calls_is_not_a_concurrency_loophole():
    async def run(ctx, values):
        try:
            await asyncio.gather(ctx.call("calculate", values))
        except ExecutionFailure:
            pass
        return {}

    env = harness(run)
    assert_failure(env, "CODE_WORKFLOW_CONCURRENT_CALL")
    assert not env.checkpoints.activity_rows("calculate")


def test_step_cannot_nest_sdk_calls_even_if_it_retains_context():
    retained = []

    async def step(_ctx, values):
        try:
            await retained[0].call("calculate", values)
        except ExecutionFailure:
            pass
        return values

    async def run(ctx, values):
        retained.append(ctx)
        return (await ctx.call("calculate", values)).require_value()

    assert_failure(harness(run, steps={"calculate": step}), "CODE_WORKFLOW_CONCURRENT_CALL")


def test_detached_request_is_cancelled_without_starting_io_and_context_closes():
    retained, tasks, called = [], [], []

    async def step(_ctx, values):
        called.append(True)
        return values

    async def run(ctx, values):
        retained.append(ctx)
        tasks.append(asyncio.create_task(ctx.call("calculate", values)))
        return values

    async def scenario(env):
        with pytest.raises(ExecutionFailure, match="CODE_WORKFLOW_CALL_INCOMPLETE"):
            await env.executor(env.claim)
        assert tasks[0].done() and not called
        with pytest.raises(ExecutionFailure, match="CODE_WORKFLOW_CONTEXT_CLOSED"):
            await retained[0].call("calculate", {})

    asyncio.run(scenario(harness(run, steps={"calculate": step})))


@pytest.mark.parametrize("code,continued", [
    ("MODEL_UPSTREAM_UNAVAILABLE", True), ("MODEL_RESPONSE_INVALID", True),
    ("EXECUTION_NOT_AUTHORIZED", False), ("TOOL_BINDING_UNAVAILABLE", False),
    ("CODE_WORKFLOW_CHECKPOINT_RESULT_UNKNOWN", False), ("UNRECOGNIZED_FAILURE", False),
])
def test_continue_policy_only_accepts_explicit_settled_operational_errors(code, continued):
    async def run(ctx, values):
        outcome = await ctx.call("review", values)
        assert outcome.status == "unavailable" and outcome.value is None and outcome.error_code == code
        return {"available": False}

    env = harness(run, steps={}, activities=[{
        "key": "review", "kind": "agent", "capability": {"key": "reviewer", "version": "1"},
        "on_unavailable": "continue",
    }], adapters=[Adapter("agent", ExecutionFailure(code))])
    if continued:
        assert execute(env) == {"available": False}
        assert env.checkpoints.activity_rows("review")[0].status == "unavailable"
    else:
        with pytest.raises(ExecutionFailure, match=code):
            execute(env)


def test_pure_python_cannot_forge_unavailable_outcome():
    async def step(_ctx, _values):
        raise ExecutionFailure("MODEL_UPSTREAM_UNAVAILABLE")

    env = harness(steps={"calculate": step}, activities=[{
        "key": "calculate", "kind": "step", "on_unavailable": "continue",
    }])
    assert_failure(env, "MODEL_UPSTREAM_UNAVAILABLE")


def test_failed_group_cancels_and_joins_siblings_before_business_can_catch_failure():
    started = asyncio.Event()
    stopped = []

    async def step(_ctx, values):
        if values["slow"]:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)
        else:
            await started.wait()
            raise RuntimeError("private payload")

    async def run(ctx, _values):
        try:
            await ctx.map("calculate", [{"slow": True}, {"slow": False}])
        except ExecutionFailure:
            assert stopped == [True]
        return {}

    env = harness(run, steps={"calculate": step}, activities=[{"key": "calculate", "kind": "step", "max_calls": 2}])
    assert_failure(env, "CODE_WORKFLOW_NODE_FAILED")
    rows = env.checkpoints.activity_rows("calculate")
    assert {row.status for row in rows} == {"failed", "cancelled"}
    assert next(row for row in rows if row.status == "cancelled").error_code == "CODE_ACTIVITY_GROUP_CANCELLED"


@pytest.mark.parametrize("control", [TaskCancellationRequested, TaskLeaseLost, ExecutionResourceUnstoppable])
def test_control_exception_cannot_be_swallowed_or_converted(control):
    async def step(_ctx, _values):
        raise control("control, not business failure")

    async def run(ctx, values):
        try:
            await ctx.call("calculate", values)
        except control:
            pass
        return {}

    env = harness(run, steps={"calculate": step})
    with pytest.raises(control):
        execute(env)
    assert all(row.status == "running" for row in env.checkpoints.rows.values())


def test_global_group_cancellation_joins_io_without_local_cancellation_receipts():
    started, stopped = asyncio.Event(), []

    async def step(_ctx, _values):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    async def run(ctx, _values):
        await ctx.map("calculate", [{}, {}])
        return {}

    async def scenario(env):
        task = asyncio.create_task(env.executor(env.claim))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped
        assert all(row.status == "running" for row in env.checkpoints.rows.values())

    asyncio.run(scenario(harness(run, steps={"calculate": step}, activities=[
        {"key": "calculate", "kind": "step", "max_calls": 2},
    ])))


@pytest.mark.parametrize("swallow", [True, False])
def test_activity_timeout_cannot_be_swallowed(swallow):
    async def step(_ctx, _values):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            if not swallow:
                raise
        return {}

    env = harness(steps={"calculate": step}, activities=[{"key": "calculate", "kind": "step", "timeout_seconds": 0.01}])
    assert_failure(env, "CODE_WORKFLOW_NODE_TIMEOUT", status="timed_out")


def test_inherited_root_deadline_is_not_caller_cancellation():
    async def step(_ctx, _values):
        await asyncio.sleep(10)

    env = harness(steps={"calculate": step})
    env.claim = env.claim.with_timeout(0.01)
    assert_failure(env, "CODE_WORKFLOW_TIMEOUT", status="timed_out")


@pytest.mark.parametrize("stage", ["begin", "activity", "result"])
def test_unknown_checkpoint_commit_is_never_retried_or_overwritten(stage, monkeypatch):
    env = harness()
    attempts = []
    if stage == "begin":
        begin = env.checkpoints.begin

        def uncertain_begin(*args, **kwargs):
            begin(*args, **kwargs)
            raise RuntimeError("private connection outcome")

        monkeypatch.setattr(env.checkpoints, "begin", uncertain_begin)
    else:
        finish = env.checkpoints.finish

        def uncertain_finish(lease, node_id, **kwargs):
            attempts.append((node_id, kwargs["status"]))
            answer = finish(lease, node_id, **kwargs)
            is_result = node_id == "receipt-workflow-result"
            if is_result == (stage == "result"):
                raise RuntimeError("private connection outcome")
            return answer

        monkeypatch.setattr(env.checkpoints, "finish", uncertain_finish)
    with pytest.raises(ExecutionFailure, match="^CODE_WORKFLOW_CHECKPOINT_RESULT_UNKNOWN$"):
        execute(env)
    assert all(status == "succeeded" for _, status in attempts)
    assert len(attempts) == (0 if stage == "begin" else 1 if stage == "activity" else 2)


@pytest.mark.parametrize("stage", ["activity", "result"])
def test_deadline_during_success_settlement_never_writes_conflicting_terminal_state(stage, monkeypatch):
    import time

    env = harness(activities=[{"key": "calculate", "kind": "step", "timeout_seconds": 0.02 if stage == "activity" else 1.0}])
    if stage == "result":
        env.claim = env.claim.with_timeout(0.03)
    finish, writes = env.checkpoints.finish, []

    def delayed_success(lease, node_id, **kwargs):
        writes.append((node_id, kwargs["status"]))
        answer = finish(lease, node_id, **kwargs)
        if (node_id == "receipt-workflow-result") == (stage == "result"):
            time.sleep(0.06)
        return answer

    monkeypatch.setattr(env.checkpoints, "finish", delayed_success)
    expected = "CODE_WORKFLOW_NODE_TIMEOUT" if stage == "activity" else "CODE_WORKFLOW_TIMEOUT"
    with pytest.raises(ExecutionFailure, match=f"^{expected}$"):
        execute(env)
    target = "receipt-workflow-result" if stage == "result" else env.checkpoints.activity_rows("calculate")[0].id
    assert [(node_id, status) for node_id, status in writes if node_id == target] == [(target, "succeeded")]


def test_unknown_terminal_commit_during_deadline_remains_unknown_not_a_safe_timeout(monkeypatch):
    import time

    env = harness(activities=[{"key": "calculate", "kind": "step", "timeout_seconds": 0.01}])
    finish, writes = env.checkpoints.finish, []

    def uncertain_finish(lease, node_id, **kwargs):
        writes.append((node_id, kwargs["status"]))
        finish(lease, node_id, **kwargs)
        time.sleep(0.03)
        raise RuntimeError("private commit uncertainty")

    monkeypatch.setattr(env.checkpoints, "finish", uncertain_finish)
    with pytest.raises(ExecutionFailure, match="^CODE_WORKFLOW_CHECKPOINT_RESULT_UNKNOWN$"):
        execute(env)
    assert len(writes) == 1 and writes[0][1] == "succeeded"
    assert env.checkpoints.rows[CODE_WORKFLOW_RESULT_KEY].status == "running"


@pytest.mark.parametrize("stage", ["node", "result", "schema", "validator", "aggregate"])
def test_invalid_outputs_never_get_success_result_evidence(stage):
    async def step(_ctx, values):
        if stage == "node":
            return {"invalid": float("nan")}
        if stage == "aggregate":
            return {"large": "x" * 600_000}
        return values

    async def run(ctx, values):
        await ctx.call("calculate", values)
        if stage == "result":
            return ["not an object"]
        if stage == "aggregate":
            return {"large": "y" * 600_000}
        return {}

    env = harness(run, steps={"calculate": step}, output_schema={"type": "object", "required": ["missing"]})
    code = "CODE_WORKFLOW_NODE_OUTPUT_INVALID" if stage == "node" else "CODE_WORKFLOW_OUTPUT_INVALID"
    if stage == "validator":
        env.validation.error = JSONValidationUnavailable("VALIDATION_CAPACITY")
        code = "CODE_WORKFLOW_VALIDATION_UNAVAILABLE"
    assert_failure(env, code)


def test_nested_code_workflow_uses_same_lease_actual_child_scope_and_matching_receipt_hashes():
    child_ref = CodeReference(key="child-code", version="1", digest="b" * 64)
    child_artifact = artifact([{"key": "calculate", "kind": "step"}], reference=child_ref, identity="child")

    async def run(ctx, values):
        prepared = (await ctx.call("prepare", values)).require_value()
        return (await ctx.call("child-call", prepared)).require_value()

    env = harness(run, steps={"prepare": identity}, activities=[{"key": "prepare", "kind": "step"}, {
        "key": "child-call", "kind": "workflow", "capability": {"key": "child", "version": "1"},
    }])

    class Catalog:
        def get(self, project, key):
            assert project == "project" and key == "child"
            return {"id": "child", "kind": "workflow", "project_key": "project", "key": "child"}

        def get_version(self, project, key, version):
            assert project == "project" and key == "child" and version == "1"
            return child_artifact

    env.checkpoints.artifacts["child"] = child_artifact
    registry = CodeWorkflowRegistry([
        InstalledCodeWorkflow(REFERENCE, run, {"prepare": identity}), InstalledCodeWorkflow(child_ref, call_identity, {"calculate": identity}),
    ])
    env.executor = CodeWorkflowExecutor(env.checkpoints, ExecutionResources(Catalog(), env.authorization, env.validation),
                                        registry, WorkflowNodeRegistry())
    assert execute(env) == {"amount": 12}
    call = env.checkpoints.activity_rows("child-call")[0]
    result = next(row for row in env.checkpoints.rows.values() if row.kind == "result" and row.parent_node_id == call.id)
    assert result.node_key == "result." + hashlib.sha256(call.id.encode()).hexdigest()
    assert result.scope.workflow_key == "child" and result.scope.content_hash == child_artifact.content_hash
    assert result.output_hash == call.output_hash
    child_step = env.checkpoints.activity_rows("calculate")[0]
    assert child_step.parent_node_id == result.id and child_step.scope.workflow_key == "child"
    assert all(claim.lease is env.claim.lease and claim.task is env.claim.task for claim in env.authorization.checked)
    assert env.authorization.resources[0][1:] == ("workflow:invoke", "child")
    require_completion_evidence(
        CompletionTarget("flow", "workflow", "1", env.claim.artifact.content_hash, capability_key="flow"),
        env.claim.artifact, CompletionCommand.prepare(status="succeeded", result={"amount": 12}),
        CompletionEvidence(nodes=tuple(NodeCompletionEvidence(
            row.id, row.node_key, row.kind, row.status, row.output_hash,
            row.parent_node_id, row.scope, row.error_code,
        ) for row in env.checkpoints.rows.values()), workflow_artifacts=(("child", child_artifact),)),
    )


@pytest.mark.parametrize("fatal_code", ["MODEL_ACCOUNTING_RESULT_UNKNOWN", "TOOL_ACCOUNTING_RESULT_UNKNOWN"])
def test_fatal_sibling_cleanup_cannot_be_degraded_through_a_child_workflow(fatal_code):
    child_ref = CodeReference(key="child-code", version="1", digest="b" * 64)
    child_artifact = artifact([{"key": "read", "kind": "tool", "max_calls": 2,
                                "capability": {"key": "lookup", "version": "1"}}],
                              reference=child_ref, identity="child")
    started, continued = asyncio.Event(), []

    class FailingReads(Adapter):
        async def __call__(self, claim, node, values, node_id):
            if values["operation"] == "safe-failure":
                await started.wait()
                raise ExecutionFailure("MODEL_UPSTREAM_UNAVAILABLE")
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ExecutionFailure(fatal_code) from None

    async def child(ctx, _values):
        await ctx.parallel({
            "first": ctx.plan("read", {"operation": "safe-failure"}, key="first"),
            "sibling": ctx.plan("read", {"operation": "cleanup-failure"}, key="sibling"),
        })
        return {}

    async def run(ctx, values):
        outcome = await ctx.call("child-call", values)
        continued.append(outcome.status)
        return {"status": outcome.status}

    env = harness(run, steps={}, activities=[{
        "key": "child-call", "kind": "workflow", "capability": {"key": "child", "version": "1"},
        "on_unavailable": "continue",
    }])
    catalog = SimpleNamespace(get=lambda *_: {"id": "child", "kind": "workflow", "project_key": "project", "key": "child"},
                              get_version=lambda *_: child_artifact)
    env.checkpoints.artifacts["child"] = child_artifact
    registry = CodeWorkflowRegistry([
        InstalledCodeWorkflow(REFERENCE, run, {}), InstalledCodeWorkflow(child_ref, child, {}),
    ])
    env.executor = CodeWorkflowExecutor(env.checkpoints, ExecutionResources(catalog, env.authorization, env.validation),
                                        registry, WorkflowNodeRegistry([FailingReads()]))
    assert_failure(env, fatal_code)
    assert continued == []
    assert all(row.status != "running" for row in env.checkpoints.rows.values())
    assert env.checkpoints.activity_rows("child-call")[0].status == "failed"


@pytest.mark.parametrize("control", [TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable])
@pytest.mark.parametrize("reverse", [False, True])
def test_join_preserves_execution_fences_regardless_of_sibling_order(control, reverse):
    from orchestration.code_workflow import _CheckpointResultUnknown, _Invocation, _SettledActivityFailure

    async def run():
        invocation = object.__new__(_Invocation)
        invocation.failure = _SettledActivityFailure("MODEL_UPSTREAM_UNAVAILABLE")

        async def fail(error):
            raise error

        failures = [control("stop"), _CheckpointResultUnknown()]
        if reverse:
            failures.reverse()
        await invocation.join([asyncio.create_task(fail(error)) for error in failures])
        assert isinstance(invocation.failure, control)

    asyncio.run(run())


@pytest.mark.parametrize("external", [True, False])
def test_child_unavailable_requires_gateway_provenance_not_step_exception(external):
    child_ref = CodeReference(key="child-code", version="1", digest="b" * 64)
    activity = ({"key": "calculate", "kind": "agent", "capability": {"key": "reviewer", "version": "1"}}
                if external else {"key": "calculate", "kind": "step"})
    child_artifact = artifact([activity], reference=child_ref, identity="child")

    async def forged(_ctx, _values):
        raise ExecutionFailure("MODEL_UPSTREAM_UNAVAILABLE")

    async def run(ctx, values):
        result = await ctx.call("child-call", values)
        assert result.status == "unavailable"
        return {"available": False}

    env = harness(run, steps={}, activities=[{
        "key": "child-call", "kind": "workflow", "capability": {"key": "child", "version": "1"},
        "on_unavailable": "continue",
    }])
    catalog = SimpleNamespace(get=lambda *_: {"id": "child", "kind": "workflow", "project_key": "project", "key": "child"},
                              get_version=lambda *_: child_artifact)
    env.checkpoints.artifacts["child"] = child_artifact
    registry = CodeWorkflowRegistry([
        InstalledCodeWorkflow(REFERENCE, run, {}),
        InstalledCodeWorkflow(child_ref, call_identity, {} if external else {"calculate": forged}),
    ])
    env.executor = CodeWorkflowExecutor(
        env.checkpoints, ExecutionResources(catalog, env.authorization, env.validation), registry,
        WorkflowNodeRegistry([Adapter("agent", ExecutionFailure("MODEL_UPSTREAM_UNAVAILABLE"))] if external else []),
    )
    if external:
        assert execute(env) == {"available": False}
        assert env.checkpoints.activity_rows("child-call")[0].status == "unavailable"
    else:
        assert_failure(env, "MODEL_UPSTREAM_UNAVAILABLE")
    assert all(row.status != "running" for row in env.checkpoints.rows.values())


def test_group_cancellation_closes_nested_frame_and_its_started_activity():
    child_ref = CodeReference(key="child-code", version="1", digest="b" * 64)
    child_artifact = artifact([{"key": "calculate", "kind": "step"}], reference=child_ref, identity="child")
    started, stopped = asyncio.Event(), []

    async def slow(_ctx, _values):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    async def fail(_ctx, _values):
        await started.wait()
        raise RuntimeError("private payload")

    async def run(ctx, values):
        await ctx.parallel({"child": ctx.plan("child-call", values), "fail": ctx.plan("fail", {})})
        return {}

    env = harness(run, steps={"fail": fail}, activities=[
        {"key": "child-call", "kind": "workflow", "capability": {"key": "child", "version": "1"}},
        {"key": "fail", "kind": "step"},
    ])
    catalog = SimpleNamespace(get=lambda *_: {"id": "child", "kind": "workflow", "project_key": "project", "key": "child"},
                              get_version=lambda *_: child_artifact)
    env.checkpoints.artifacts["child"] = child_artifact
    registry = CodeWorkflowRegistry([
        InstalledCodeWorkflow(REFERENCE, run, {"fail": fail}),
        InstalledCodeWorkflow(child_ref, call_identity, {"calculate": slow}),
    ])
    env.executor = CodeWorkflowExecutor(env.checkpoints, ExecutionResources(catalog, env.authorization, env.validation),
                                        registry, WorkflowNodeRegistry())
    assert_failure(env, "CODE_WORKFLOW_NODE_FAILED")
    assert stopped == [True]
    assert all(row.status != "running" for row in env.checkpoints.rows.values())
    child_rows = [row for row in env.checkpoints.rows.values() if row.scope.workflow_key == "child"]
    assert len(child_rows) == 2 and all(row.status == "cancelled" for row in child_rows)


def test_authorization_revocation_is_fatal_before_activity_success():
    async def step(_ctx, values):
        env.authorization.enabled = False
        return values

    env = harness(steps={"calculate": step})
    with pytest.raises(ExecutionFailure, match="EXECUTION_NOT_AUTHORIZED"):
        execute(env)
    assert all(row.status == "running" for row in env.checkpoints.rows.values())


def test_successful_receipt_is_not_replayed_in_same_live_attempt():
    env = harness()
    assert execute(env) == {"amount": 12}
    before = list(env.checkpoints.calls)
    with pytest.raises(ExecutionFailure, match="CODE_WORKFLOW_REPLAY_UNSUPPORTED"):
        execute(env)
    assert env.checkpoints.calls == before + [("begin", CODE_WORKFLOW_RESULT_KEY)]


def test_registry_is_exact_immutable_and_rejects_ambiguous_installations():
    steps = {"calculate": identity}
    installed = InstalledCodeWorkflow(REFERENCE, identity, steps)
    registry = CodeWorkflowRegistry([installed])
    steps.clear()
    assert registry.resolve(REFERENCE) is installed and tuple(installed.steps) == ("calculate",)
    with pytest.raises(TypeError):
        installed.steps["another"] = identity
    with pytest.raises(FrozenInstanceError):
        registry._installed = {}
    for other in (installed, replace(installed, reference=REFERENCE.model_copy(update={"digest": "b" * 64}))):
        with pytest.raises(CodeWorkflowRegistryError, match="Duplicate or ambiguous"):
            CodeWorkflowRegistry([installed, other])
    for reference in (REFERENCE.model_copy(update={"digest": "b" * 64}), REFERENCE.model_copy(update={"version": "2"})):
        with pytest.raises(CodeWorkflowRegistryError, match="Exact"):
            registry.resolve(reference)
    with pytest.raises(ValueError, match="async"):
        InstalledCodeWorkflow(REFERENCE, lambda *_: {}, {})


def source_package(tmp_path, monkeypatch):
    package = "audited_business_" + uuid4().hex
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").write_text("# audited package\n")
    (root / "business.py").write_text("async def run(ctx, values):\n    return values\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    return package, root


def test_explicit_source_manifest_is_deterministic_and_verified_at_startup_and_use(tmp_path, monkeypatch):
    package, root = source_package(tmp_path, monkeypatch)
    manifest = SourceManifest(package, ("__init__.py", "business.py"))
    reference = manifest.reference("reviewed", "1")
    assert SourceManifest(package, tuple(reversed(manifest.files))).reference("reviewed", "1") == reference
    (root / "unlisted.py").write_text("# explicitly not part of this integrity claim\n")
    assert manifest.reference("reviewed", "1") == reference
    installed = InstalledCodeWorkflow(reference, identity, {}, manifest)
    registry = CodeWorkflowRegistry([installed])
    assert registry.resolve(reference) is installed
    (root / "business.py").write_text("# modified reviewed bytes\n")
    with pytest.raises(CodeWorkflowRegistryError, match="digest"):
        registry.resolve(reference)
    with pytest.raises(CodeWorkflowRegistryError, match="digest"):
        CodeWorkflowRegistry([installed])


@pytest.mark.parametrize("name", ["../outside.py", "/absolute.py", "a/../file.py", "./file.py", "a//file.py", "a\\file.py", "."])
def test_manifest_rejects_traversal_and_noncanonical_resource_paths(name):
    with pytest.raises(ValueError):
        SourceManifest("framework", (name,))


def test_manifest_rejects_symlinks_missing_and_oversized_resources(tmp_path, monkeypatch):
    package, root = source_package(tmp_path, monkeypatch)
    outside = tmp_path / "outside.py"
    outside.write_text("# not audited\n")
    (root / "linked.py").symlink_to(outside)
    (root / "large.py").write_bytes(b"x" * 1_000_001)
    for resource in ("linked.py", "missing.py", "large.py"):
        with pytest.raises(CodeWorkflowRegistryError):
            SourceManifest(package, (resource,)).reference("reviewed", "1")
