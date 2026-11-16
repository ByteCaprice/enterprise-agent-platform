"""Execute reviewed Python with bounded, task-owned activity calls.

Python owns business control flow, never leases, gateways or detached execution.
This is a cooperative code contract, not a sandbox for hostile Python.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Coroutine, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from domain.business_code import (
    CODE_WORKFLOW_CHECKPOINT_PROFILE,
    CODE_WORKFLOW_INTERPRETER_PROFILE,
    CODE_WORKFLOW_RESULT_KEY,
    MAX_CODE_FRAME_DEPTH,
    MAX_CODE_NODES,
    ActivityCall,
    ActivityOutcome,
    CodeNodeScope,
    CodeWorkflowDefinition,
    is_activity_unavailable,
)
from domain.capability import CapabilityKind
from domain.capability_definitions import interpret_definition
from domain.execution_ports import ExecutionResources, WorkflowCheckpoints
from domain.execution_requirements import WORKFLOW_ADAPTER_PROFILES, ExecutionProfile
from domain.schema_validation import JSONValidationError, JSONValidationUnavailable
from domain.task import TaskError, canonical_json
from domain.task_execution import (
    ExecutionFailure,
    ExecutionResourceUnstoppable,
    TaskCancellationRequested,
    TaskClaim,
    TaskLeaseLost,
)
from domain.workflow import WorkflowNode
from domain.workflow_invocation import WorkflowInvocation
from domain.workflow_runtime import WorkflowAdapterUnavailable, WorkflowNodeRegistry
from framework.business_code import (
    BusinessStepContext,
    CodeWorkflowRegistry,
    CodeWorkflowRegistryError,
    InstalledCodeWorkflow,
)
from orchestration.published_capabilities import PublishedCapabilities

_CONTROL_ERRORS = (asyncio.CancelledError, TaskCancellationRequested, TaskLeaseLost, ExecutionResourceUnstoppable)
_ROOT_TIMEOUT = 86400


class _CheckpointResultUnknown(ExecutionFailure):
    def __init__(self):
        super().__init__("CODE_WORKFLOW_CHECKPOINT_RESULT_UNKNOWN")


class _SettledActivityFailure(ExecutionFailure):
    """Provenance carried through failed child frames, never forged by a step."""


async def _blocking(function, *args, _on_cancel_result=None, _on_cancel_error=None, **kwargs):
    """Drain synchronous ports on cancellation; never offload business code.

    The owning Worker's cancellation grace bounds an uncooperative port. We must
    not return and let that Worker release authority while its IO is still live.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            failure = task.exception()
            if failure is not None and _on_cancel_error is not None:
                _on_cancel_error(failure)
            if failure is None and _on_cancel_result is not None:
                _on_cancel_result(task.result())
        raise


def _checkpoint_cancel_error(failure):
    # Draining a cancelled transaction does not make a connection error known.
    # Never hide that uncertainty behind a timeout or a sibling's safe failure.
    if isinstance(failure, _CONTROL_ERRORS):
        raise failure
    raise _CheckpointResultUnknown() from None


def _object(value, code: str) -> dict:
    try:
        if type(value) is not dict:
            raise TaskError("Object required")
        return json.loads(canonical_json(value))
    except TaskError:
        raise ExecutionFailure(code) from None


def _hash(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class _TreeBudget:
    nodes: int = 0
    outputs: dict = field(default_factory=dict)
    owner: asyncio.Task = field(default_factory=asyncio.current_task)
    group_cancelled: set[asyncio.Task] = field(default_factory=set)

    def reserve(self, count):
        if self.nodes + count > MAX_CODE_NODES:
            raise ExecutionFailure("CODE_WORKFLOW_NODE_LIMIT")
        self.nodes += count

    def output(self, key, value):
        try:
            canonical_json(self.outputs | {key: value})
        except TaskError:
            raise ExecutionFailure("CODE_WORKFLOW_OUTPUT_INVALID") from None
        self.outputs[key] = value


# Descendants share only accounting, not an execution owner or a second Task.
_TREE: ContextVar[_TreeBudget | None] = ContextVar("code_workflow_tree", default=None)
_DEPTH: ContextVar[int] = ContextVar("code_workflow_depth", default=0)


class BusinessWorkflowContext:
    """Activity SDK; only platform-owned groups may create execution Tasks."""

    __slots__ = ("__invocation",)

    def __init__(self, invocation: _Invocation):
        self.__invocation = invocation

    def plan(self, activity: str, values: dict, *, key: str = "once") -> ActivityCall:
        return self.__invocation.plan(activity, values, key=key)

    def call(self, activity: str, values: dict, *, key: str = "once") -> Coroutine[Any, Any, ActivityOutcome]:
        return self.__invocation.request({"once": self.plan(activity, values, key=key)}, mode="single")

    def parallel(self, calls: Mapping[str, ActivityCall], *, concurrency: int | None = None):
        return self.__invocation.request(calls, concurrency=concurrency)

    def map(self, activity: str, items: list[dict], *, key=None, concurrency: int | None = None):
        invocation = self.__invocation
        try:
            invocation.guard()
            if type(items) is not list or len(items) > MAX_CODE_NODES or (key is not None and not callable(key)):
                invocation.reject("CODE_WORKFLOW_GROUP_INVALID")
            if activity not in invocation.activities:
                invocation.reject("CODE_WORKFLOW_ACTIVITY_UNKNOWN")
            calls = {str(index): invocation.plan(activity, values, key=str(index) if key is None else key(values))
                     for index, values in enumerate(items)}
            return invocation.request(calls, mode="list", concurrency=concurrency)
        except BaseException as exc:
            invocation.poison(exc)
            raise

    def skip(self, activity: str, *, key: str = "once") -> Coroutine[Any, Any, ActivityOutcome]:
        return self.__invocation.request({"once": self.plan(activity, {}, key=key)}, mode="single", skip=True)


class CodeWorkflowExecutor:
    def __init__(self, checkpoints: WorkflowCheckpoints, resources: ExecutionResources,
                 registry: CodeWorkflowRegistry, adapters: WorkflowNodeRegistry):
        self._checkpoints = checkpoints
        self._resources = resources
        self._registry = registry
        self._adapters = adapters
        self._catalog = PublishedCapabilities(resources.capabilities)

    @property
    def additional_profiles(self) -> tuple[ExecutionProfile, ...]:
        return (ExecutionProfile("interpreter", "workflow_code", CODE_WORKFLOW_INTERPRETER_PROFILE),
                ExecutionProfile("checkpoint", "workflow_code", CODE_WORKFLOW_CHECKPOINT_PROFILE))

    @property
    def implementation_references(self):
        return self._registry.references

    @property
    def workflow_implementation_references(self):
        return self._registry.references

    def _prepare(self, claim: WorkflowInvocation) -> tuple[CodeWorkflowDefinition, InstalledCodeWorkflow]:
        try:
            if claim.task.capability_kind != "workflow":
                raise ValueError("Not a Workflow")
            claim.artifact.require_published(version=claim.reference.version)
            contract = interpret_definition("workflow", claim.artifact)
            definition = contract.materialize()
            if contract.profile != CODE_WORKFLOW_INTERPRETER_PROFILE or not isinstance(definition, CodeWorkflowDefinition):
                raise ValueError("Not a code Workflow")
        except Exception:
            raise ExecutionFailure("CODE_WORKFLOW_DEFINITION_INVALID") from None
        try:
            installed = self._registry.resolve(definition.implementation)
        except CodeWorkflowRegistryError:
            raise ExecutionFailure("CODE_WORKFLOW_IMPLEMENTATION_UNAVAILABLE") from None
        if set(installed.steps) != {activity.key for activity in definition.activities if activity.kind == "step"}:
            raise ExecutionFailure("CODE_WORKFLOW_IMPLEMENTATION_MISMATCH")
        try:
            required = {activity.kind for activity in definition.activities if activity.kind not in {"step", "workflow"}}
            self._adapters.require(required)
            if any(self._adapters.get(kind).descriptor.profile != WORKFLOW_ADAPTER_PROFILES[kind] for kind in required):
                raise WorkflowAdapterUnavailable("Incompatible adapter profile")
        except WorkflowAdapterUnavailable:
            raise ExecutionFailure("WORKFLOW_ADAPTER_UNAVAILABLE") from None
        return definition, installed

    async def _authorized(self, claim):
        if not await _blocking(self._resources.authorization.allowed, claim):
            raise ExecutionFailure("EXECUTION_NOT_AUTHORIZED")

    async def _begin(self, claim, key, source, *, scope, parent_node_id, on_cancel_checkpoint=None):
        await self._authorized(claim)
        try:
            checkpoint = await _blocking(
                self._checkpoints.begin, claim.lease, key, _hash(source),
                interpreter_profile=CODE_WORKFLOW_INTERPRETER_PROFILE,
                checkpoint_profile=CODE_WORKFLOW_CHECKPOINT_PROFILE,
                scope=scope, parent_node_id=parent_node_id,
                _on_cancel_result=on_cancel_checkpoint,
                _on_cancel_error=_checkpoint_cancel_error,
            )
        except ValueError:
            raise ExecutionFailure("WORKFLOW_CHECKPOINT_CONFLICT") from None
        except _CONTROL_ERRORS:
            raise
        except Exception:
            raise _CheckpointResultUnknown() from None
        if checkpoint is None:
            raise TaskLeaseLost("Code Workflow no longer owns its execution lease")
        if not checkpoint.created:
            raise ExecutionFailure("CODE_WORKFLOW_REPLAY_UNSUPPORTED")
        return checkpoint

    async def _finish(self, claim, checkpoint, *, status, output=None, error_code=None):
        await self._authorized(claim)
        try:
            finished = await _blocking(self._checkpoints.finish, claim.lease, checkpoint.record.id,
                                      status=status, output=output, error_code=error_code,
                                      _on_cancel_error=_checkpoint_cancel_error)
        except _CONTROL_ERRORS:
            raise
        except Exception:
            raise _CheckpointResultUnknown() from None
        if not finished:
            raise TaskLeaseLost("Code Workflow no longer owns its execution lease")

    async def __call__(self, claim: TaskClaim) -> dict:
        try:
            claim.artifact.require_published(version=claim.task.version, content_hash=claim.task.content_hash)
        except Exception:
            raise ExecutionFailure("CODE_WORKFLOW_DEFINITION_INVALID") from None
        return await self.invoke(WorkflowInvocation.from_root(claim))

    async def invoke(self, claim: WorkflowInvocation) -> dict:
        depth = _DEPTH.get() + 1
        if depth > MAX_CODE_FRAME_DEPTH:
            raise ExecutionFailure("CODE_WORKFLOW_DEPTH_LIMIT")
        tree = _TREE.get()
        tree_token = _TREE.set(tree or _TreeBudget())
        depth_token = _DEPTH.set(depth)
        try:
            return await self._invoke(claim, _TREE.get())
        finally:
            _DEPTH.reset(depth_token)
            _TREE.reset(tree_token)

    async def _invoke(self, claim, tree):
        definition, installed = await _blocking(self._prepare, claim)
        claim = claim.with_timeout(_ROOT_TIMEOUT)
        invocation = _Invocation(self, claim, definition, installed, tree)
        result = None
        result_settlement_started = False
        deadline = None

        def began_during_cancellation(receipt):
            nonlocal result
            if receipt is not None and receipt.created:
                result = receipt

        try:
            async with asyncio.timeout(claim.limit_timeout(_ROOT_TIMEOUT)) as deadline:
                values = _object(claim.input, "CODE_WORKFLOW_INPUT_INVALID")
                # Nested input has not passed the root Task's admission validation.
                if claim.parent_node_id is not None:
                    await self._validate(values, claim.artifact.input_schema, "CODE_WORKFLOW_INPUT_INVALID")
                tree.reserve(1)
                result = await self._begin(claim, invocation.result_key, invocation.source("result", values),
                                           scope=invocation.scope(), parent_node_id=claim.parent_node_id,
                                           on_cancel_checkpoint=began_during_cancellation)
                invocation.result_id = result.record.id
                output = await installed.run(BusinessWorkflowContext(invocation), values)
                await invocation.close()
                if deadline.expired():
                    raise TimeoutError
                invocation.check()
                claim.limit_timeout(_ROOT_TIMEOUT)
                if any(activity.required and activity.key not in invocation.completed for activity in definition.activities):
                    raise ExecutionFailure("CODE_WORKFLOW_REQUIRED_ACTIVITY_MISSING")
                output = _object(output, "CODE_WORKFLOW_OUTPUT_INVALID")
                tree.output(result.record.id, output)
                await self._validate(output, claim.artifact.output_schema, "CODE_WORKFLOW_OUTPUT_INVALID")
                invocation.check()
                claim.limit_timeout(_ROOT_TIMEOUT)
                result_settlement_started = True
                await self._finish(claim, result, status="succeeded", output=output)
                return _object(output, "CODE_WORKFLOW_OUTPUT_INVALID")
        except BaseException as exc:
            if (isinstance(exc, TimeoutError) and deadline is not None and deadline.expired()
                    and not isinstance(invocation.failure, (TaskCancellationRequested, TaskLeaseLost, ExecutionResourceUnstoppable))):
                invocation.failure = ExecutionFailure("CODE_WORKFLOW_TIMEOUT", status="timed_out")
            invocation.poison(exc)
            await invocation.close()
            failure = invocation.failure
            if isinstance(failure, _CONTROL_ERRORS):
                if (isinstance(failure, asyncio.CancelledError) and invocation.locally_cancelled()
                        and result is not None and not result_settlement_started):
                    result_settlement_started = True
                    await self._finish(claim, result, status="cancelled", error_code="CODE_ACTIVITY_GROUP_CANCELLED")
                raise failure from None
            if isinstance(failure, TimeoutError):
                failure = ExecutionFailure("CODE_WORKFLOW_TIMEOUT", status="timed_out")
            elif not isinstance(failure, ExecutionFailure):
                if not isinstance(failure, Exception):
                    raise failure from None
                failure = ExecutionFailure("CODE_WORKFLOW_FAILED")
            if result is not None and not result_settlement_started and not isinstance(failure, _CheckpointResultUnknown):
                await self._finish(claim, result, status="failed", error_code=failure.code)
            raise failure from None
        finally:
            await invocation.close()

    async def _validate(self, values, schema, invalid_code):
        try:
            await _blocking(self._resources.validation.validate, values, schema)
        except JSONValidationUnavailable:
            raise ExecutionFailure("CODE_WORKFLOW_VALIDATION_UNAVAILABLE") from None
        except JSONValidationError:
            raise ExecutionFailure(invalid_code) from None

    async def _child(self, claim, activity, values, node_id):
        await self._authorized(claim)
        if not await _blocking(self._resources.authorization.allowed_resource,
                               claim, "workflow:invoke", activity.capability.key):
            raise ExecutionFailure("EXECUTION_NOT_AUTHORIZED")
        try:
            artifact = await _blocking(self._catalog.resolve, claim.task.project_key,
                                       activity.capability, CapabilityKind.WORKFLOW)
            if not isinstance(interpret_definition("workflow", artifact).materialize(), CodeWorkflowDefinition):
                raise ValueError("Only code Workflow children are allowed")
        except _CONTROL_ERRORS:
            raise
        except Exception:
            raise ExecutionFailure("CODE_WORKFLOW_CHILD_INVALID") from None
        child = claim.child(activity.capability, artifact, values, node_id)
        await self._authorized(child)
        return await self.invoke(child)


class _Invocation:
    def __init__(self, executor, claim, definition, installed, tree):
        self.executor = executor
        self.claim = claim
        self.definition = definition
        self.installed = installed
        self.tree = tree
        self.activities = {activity.key: activity for activity in definition.activities}
        self.owner = asyncio.current_task()
        self.used: set[tuple[str, str]] = set()
        self.counts: Counter = Counter()
        self.completed: set[str] = set()
        self.failure: BaseException | None = None
        self.closed = False
        self.busy = False
        self.pending: Coroutine | None = None
        self.children: set[asyncio.Task] = set()
        self.result_id = None
        self.result_key = (CODE_WORKFLOW_RESULT_KEY if claim.parent_node_id is None
                           else "result." + _text_hash(claim.parent_node_id))

    def poison(self, failure):
        # Stop/quarantine and lease fences outrank every cleanup error, regardless
        # of the order in which sibling tasks happen to finish.
        controls = (TaskCancellationRequested, TaskLeaseLost, ExecutionResourceUnstoppable)
        current_rank = next((rank for rank, kind in enumerate(controls, 1) if isinstance(self.failure, kind)), 0)
        incoming_rank = next((rank for rank, kind in enumerate(controls, 1) if isinstance(failure, kind)), 0)
        if current_rank or incoming_rank:
            if incoming_rank > current_rank:
                self.failure = failure
            return
        if isinstance(failure, _CheckpointResultUnknown):
            self.failure = failure
            return
        if isinstance(self.failure, _CheckpointResultUnknown):
            return
        # A settled operational rejection is weaker than a fatal error discovered
        # while joining its siblings. Never degrade an accounting-unknown tree.
        if (isinstance(failure, asyncio.CancelledError) and self.locally_cancelled() and
                isinstance(self.failure, ExecutionFailure) and
                not isinstance(self.failure, _SettledActivityFailure)):
            return
        if (self.failure is None or isinstance(failure, _CONTROL_ERRORS) or
                (isinstance(self.failure, _SettledActivityFailure) and
                 not isinstance(failure, _SettledActivityFailure)) or
                (isinstance(self.failure, asyncio.CancelledError) and self.locally_cancelled() and
                 isinstance(failure, ExecutionFailure) and not isinstance(failure, _SettledActivityFailure))):
            self.failure = failure

    def check(self):
        if self.owner.cancelling():
            self.poison(asyncio.CancelledError())
        if self.failure is not None:
            raise self.failure

    def reject(self, code):
        failure = ExecutionFailure(code)
        self.poison(failure)
        raise failure

    def locally_cancelled(self):
        return asyncio.current_task() in self.tree.group_cancelled and not self.tree.owner.cancelling()

    def guard(self):
        if self.closed:
            self.reject("CODE_WORKFLOW_CONTEXT_CLOSED")
        self.check()
        if self.busy or asyncio.current_task() is not self.owner:
            self.reject("CODE_WORKFLOW_CONCURRENT_CALL")

    def scope(self, call=None):
        return CodeNodeScope(self.claim.reference.key, self.claim.reference.version, self.claim.artifact.content_hash,
                             call.activity if call else None, _text_hash(call.key) if call else None)

    def node_key(self, call):
        return "call." + _text_hash(self.result_id + "\0" + call.activity + "\0" + _text_hash(call.key))

    def source(self, kind, values, call=None):
        return {
            "interpreter_profile": CODE_WORKFLOW_INTERPRETER_PROFILE,
            "checkpoint_profile": CODE_WORKFLOW_CHECKPOINT_PROFILE,
            "definition": self.claim.artifact.content_hash,
            "implementation": self.installed.reference.model_dump(),
            "kind": kind, "activity": call.activity if call else None,
            "invocation_hash": _text_hash(call.key) if call else None, "input": _hash(values),
        }

    def plan(self, activity, values, *, key):
        try:
            self.guard()
            call = ActivityCall(activity, values, key=key)
            if call.activity not in self.activities:
                self.reject("CODE_WORKFLOW_ACTIVITY_UNKNOWN")
            return call
        except (ValueError, TypeError, TaskError, UnicodeError):
            self.reject("CODE_WORKFLOW_CALL_INVALID")
        except BaseException as exc:
            self.poison(exc)
            raise

    def request(self, calls, *, mode="mapping", concurrency=None, skip=False):
        try:
            self.guard()
            limit = self.definition.max_parallelism if concurrency is None else concurrency
            if type(limit) is not int or not 1 <= limit <= self.definition.max_parallelism:
                self.reject("CODE_WORKFLOW_CONCURRENCY_INVALID")
            if not isinstance(calls, Mapping) or len(calls) > MAX_CODE_NODES:
                self.reject("CODE_WORKFLOW_GROUP_INVALID")
            calls = dict(calls)
            identities = set()
            counts = Counter()
            for label, call in calls.items():
                if not isinstance(label, str) or not label or len(label) > 200 or not isinstance(call, ActivityCall):
                    self.reject("CODE_WORKFLOW_GROUP_INVALID")
                if call.activity not in self.activities:
                    self.reject("CODE_WORKFLOW_ACTIVITY_UNKNOWN")
                identity = (call.activity, _text_hash(call.key))
                if identity in identities or identity in self.used:
                    self.reject("CODE_WORKFLOW_CALL_ALREADY_USED")
                identities.add(identity)
                counts[call.activity] += 1
                if self.counts[call.activity] + counts[call.activity] > self.activities[call.activity].max_calls:
                    self.reject("CODE_WORKFLOW_CALL_LIMIT")
                if skip and self.activities[call.activity].required:
                    self.reject("CODE_WORKFLOW_REQUIRED_ACTIVITY_SKIPPED")
            try:
                canonical_json([call.values() for call in calls.values()])
            except TaskError:
                self.reject("CODE_WORKFLOW_INPUT_INVALID")
            # All validation and reservation precedes the first checkpoint or IO.
            self.tree.reserve(len(calls))
            self.used.update(identities)
            self.counts.update(counts)
            self.busy = True
            self.pending = self.drive(calls, mode=mode, concurrency=limit, skip=skip)
            return self.pending
        except BaseException as exc:
            self.poison(exc)
            raise

    async def drive(self, calls, *, mode, concurrency, skip):
        try:
            self.check()
            if self.closed:
                self.reject("CODE_WORKFLOW_CONTEXT_CLOSED")
            if asyncio.current_task() is not self.owner:
                self.reject("CODE_WORKFLOW_CONCURRENT_CALL")
            if mode == "single":
                return await self.perform(next(iter(calls.values())), skip=skip)
            results = await self.group(calls, concurrency)
            self.check()
            return list(results.values()) if mode == "list" else results
        except BaseException as exc:
            self.poison(exc)
            raise
        finally:
            self.busy = False
            self.pending = None

    async def group(self, calls, concurrency):
        pending = iter(calls.items())
        results = {}

        async def worker():
            try:
                for label, call in pending:
                    results[label] = await self.perform(call)
            except BaseException as exc:
                # Sibling cancellations must not replace the originating failure.
                if not self.locally_cancelled():
                    self.poison(exc)
                raise

        tasks = {asyncio.create_task(worker()) for _ in range(min(concurrency, len(calls)))}
        self.children.update(tasks)
        try:
            if tasks:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    if task.cancelled():
                        raise asyncio.CancelledError
                    if task.exception() is not None:
                        raise task.exception()
                self.check()
            return {label: results[label] for label in calls}
        finally:
            for task in tasks:
                if not task.done():
                    if self.locally_cancelled() or (not self.owner.cancelling() and not isinstance(self.failure, _CONTROL_ERRORS)):
                        self.tree.group_cancelled.add(task)
                    task.cancel()
            await self.join(tasks)
            self.children.difference_update(tasks)
            self.tree.group_cancelled.difference_update(tasks)

    async def join(self, tasks):
        if not tasks:
            return
        joined = asyncio.gather(*tasks, return_exceptions=True)
        while not joined.done():
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError as exc:
                self.poison(exc)
        # A control/unknown settlement raised during sibling cleanup is fatal.
        for failure in joined.result():
            if isinstance(failure, (TaskCancellationRequested, TaskLeaseLost, ExecutionResourceUnstoppable, ExecutionFailure)):
                self.poison(failure)

    async def perform(self, call: ActivityCall, *, skip=False):
        activity = self.activities[call.activity]
        checkpoint = None
        settlement_started = False
        claim = self.claim.with_timeout(activity.timeout_seconds)
        deadline = None

        def began_during_cancellation(receipt):
            nonlocal checkpoint
            if receipt is not None and receipt.created:
                checkpoint = receipt

        try:
            async with asyncio.timeout(claim.limit_timeout(activity.timeout_seconds)) as deadline:
                self.check()
                values = call.values()
                checkpoint = await self.executor._begin(
                    claim, self.node_key(call), self.source("skip" if skip else activity.kind, values, call),
                    scope=self.scope(call), parent_node_id=self.result_id,
                    on_cancel_checkpoint=began_during_cancellation)
                output = None
                if not skip:
                    if activity.kind == "step":
                        metadata = BusinessStepContext(
                            project_key=claim.task.project_key, submitted_by=claim.task.submitted_by,
                            task_id=claim.task.id, task_attempt_id=claim.lease.attempt_id, node_id=checkpoint.record.id,
                        )
                        output = await self.installed.steps[activity.key](metadata, values)
                    elif activity.kind == "workflow":
                        output = await self.executor._child(claim, activity, values, checkpoint.record.id)
                    else:
                        external = WorkflowNode(key=activity.key, kind=activity.kind, capability=activity.capability,
                                                prompt=activity.prompt, timeout_seconds=activity.timeout_seconds)
                        try:
                            output = await self.executor._adapters.get(activity.kind).adapter(
                                claim, external, values, checkpoint.record.id)
                        except ExecutionFailure as exc:
                            if is_activity_unavailable(exc.code):
                                raise _SettledActivityFailure(exc.code, status=exc.status) from None
                            raise
                    output = _object(output, "CODE_WORKFLOW_NODE_OUTPUT_INVALID")
                    self.tree.output(checkpoint.record.id, output)
                if deadline.expired():
                    raise TimeoutError
                self.check()
                claim.limit_timeout(activity.timeout_seconds)
                status = "skipped" if skip else "succeeded"
                settlement_started = True
                await self.executor._finish(claim, checkpoint, status=status, output=output)
                if not skip:
                    self.completed.add(activity.key)
                return ActivityOutcome(status, output)
        except asyncio.CancelledError:
            if self.locally_cancelled() and checkpoint is not None and not settlement_started:
                settlement_started = True
                await self.executor._finish(claim, checkpoint, status="cancelled", error_code="CODE_ACTIVITY_GROUP_CANCELLED")
            raise
        except _CONTROL_ERRORS:
            raise
        except _CheckpointResultUnknown:
            # A terminal transaction may have committed: never retry or overwrite.
            raise
        except BaseException as exc:
            if isinstance(exc, TimeoutError):
                failure = ExecutionFailure("CODE_WORKFLOW_NODE_TIMEOUT", status="timed_out")
            elif isinstance(exc, ExecutionFailure):
                failure = exc
            elif isinstance(exc, Exception):
                failure = ExecutionFailure("CODE_WORKFLOW_NODE_FAILED")
            else:
                raise
            # Only a gateway's settled, explicit operational failure can continue;
            # pure Python exceptions and checkpoint failures never become values.
            unavailable = (not skip and activity.kind != "step" and activity.on_unavailable == "continue"
                           and isinstance(failure, _SettledActivityFailure) and not settlement_started)
            if checkpoint is not None and not settlement_started:
                settlement_started = True
                await self.executor._finish(claim, checkpoint, status="unavailable" if unavailable else "failed",
                                            error_code=failure.code)
            if unavailable and checkpoint is not None:
                self.check()
                self.completed.add(activity.key)
                return ActivityOutcome("unavailable", error_code=failure.code)
            raise failure from None

    async def close(self):
        self.closed = True
        if self.busy:
            self.poison(ExecutionFailure("CODE_WORKFLOW_CALL_INCOMPLETE"))
        pending = self.pending
        if pending is not None:
            tasks = {task for task in asyncio.all_tasks()
                     if task is not asyncio.current_task() and task.get_coro() is pending}
            tasks.update(self.children)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await self.join(tasks)
            if not tasks:
                pending.close()
            self.pending = None
            self.busy = False
