"""Lease-fenced new-platform worker, with explicitly installed execution adapters.

No dynamic imports from published artifacts or implicit default executor.
Adapters must be async/cancellation-cooperative and enforce model/tool permissions at
actual calls. An adapter that will not stop poisons the worker; it must not claim again.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from domain.execution_context import ExecutionDeadlineExceeded
from domain.schema_validation import JSONValidationUnavailable, ensure_self_contained_schema
from domain.task import TaskValidationUnavailable, canonical_json
from domain.task_control import ControlState, ExecutionObservation
from domain.task_execution import (
    CompletionOutcome,
    ExecutionFailure,
    TaskCancellationRequested,
    TaskClaim,
    TaskLeaseLost,
)
from domain.task_execution import ExecutionResourceUnstoppable as WorkerExecutionUnstoppable
from infra.schema_validation import validate_json
from orchestration.execution_authorization import ExecutionAuthorizer

Executor = Callable[[TaskClaim], Awaitable[dict]]


@dataclass(frozen=True)
class WorkerPolicy:
    lease_seconds: float = 30
    heartbeat_seconds: float = 5
    execution_timeout_seconds: float = 300
    cancellation_grace_seconds: float = 5
    poll_seconds: float = 1

    def __post_init__(self):
        values = (self.lease_seconds, self.heartbeat_seconds, self.execution_timeout_seconds,
                  self.cancellation_grace_seconds, self.poll_seconds)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("Worker timing values must be positive finite numbers")
        if not 1 <= self.lease_seconds <= 3600 or self.heartbeat_seconds >= self.lease_seconds / 2:
            raise ValueError("Heartbeat must be less than half the bounded lease duration")
        if self.execution_timeout_seconds > 86400 or self.cancellation_grace_seconds > 60:
            raise ValueError("Worker execution and cancellation durations exceed policy bounds")


class PlatformWorker:
    def __init__(self, store, worker_id: str, executors: Mapping[str, Executor], *, policy: WorkerPolicy | None = None,
                 workflow_agents: bool = False):
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 200:
            raise ValueError("Invalid worker_id")
        if not executors or not set(executors) <= {"agent", "workflow"} or not all(callable(e) for e in executors.values()):
            raise ValueError("Install explicit Agent/Workflow execution adapters")
        self.store = store
        self.worker_id = worker_id
        self.executors = MappingProxyType(dict(executors))
        self.policy = policy or WorkerPolicy()
        if type(workflow_agents) is not bool or (workflow_agents and "workflow" not in executors):
            raise ValueError("Declare Workflow Agent support on a Workflow Worker")
        node_kinds = getattr(self.executors.get("workflow"), "node_kinds", None)
        if node_kinds is not None and (
            not isinstance(node_kinds, frozenset) or not node_kinds <= {"rule", "llm", "tool", "agent"} or
            ("agent" in node_kinds) != workflow_agents
        ):
            raise ValueError("Workflow Agent role must match the installed node adapter catalog")
        self.workflow_agents = workflow_agents
        self.authorizer = ExecutionAuthorizer(store)
        self._busy = False
        self._poisoned = False

    async def _stop(self, execution):
        if execution is None:
            return
        if not execution.done():
            execution.cancel()
        try:
            done, _ = await asyncio.wait({execution}, timeout=self.policy.cancellation_grace_seconds)
        except BaseException:
            # A second shutdown cancellation must not release a still-running adapter.
            self._poisoned = True
            raise
        if not done:
            self._poisoned = True
            raise WorkerExecutionUnstoppable("Execution adapter did not stop; terminate worker")
        # Retrieve failures without logging potentially sensitive adapter exceptions.
        if not execution.cancelled():
            failure = execution.exception()
            if isinstance(failure, WorkerExecutionUnstoppable):
                self._poisoned = True
                raise failure

    def _require_budget(self, claim):
        try:
            claim.limit_timeout(self.policy.execution_timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("EXECUTION_TIMEOUT_RESULT_UNKNOWN", status="timed_out") from None

    async def _execute(self, claim):
        self._require_budget(claim)
        artifact = claim.artifact
        artifact.require_published(version=claim.task.version, content_hash=claim.task.content_hash)
        for schema in (artifact.input_schema, artifact.output_schema):
            ensure_self_contained_schema(schema)
        canonical_json(claim.input)
        try:
            await asyncio.to_thread(validate_json, claim.input, artifact.input_schema)
        except JSONValidationUnavailable:
            raise ExecutionFailure("TASK_VALIDATION_UNAVAILABLE") from None
        # Never import an executable target supplied in a capability definition.
        result = await self.executors[claim.task.capability_kind](claim)
        self._require_budget(claim)
        if not isinstance(result, dict):
            raise ValueError("Execution output must be a JSON object")
        canonical_json(result)
        try:
            await asyncio.to_thread(validate_json, result, artifact.output_schema)
        except JSONValidationUnavailable:
            raise ExecutionFailure("TASK_VALIDATION_UNAVAILABLE") from None
        self._require_budget(claim)
        return result

    async def _control(self, claim):
        return await asyncio.wait_for(asyncio.to_thread(
            self.store.task_control.observe, claim.lease, lease_seconds=self.policy.lease_seconds,
            cancellation_grace_seconds=self.policy.cancellation_grace_seconds,
        ), timeout=self.policy.lease_seconds / 3)

    async def _renew(self, claim) -> ExecutionObservation:
        async def check():
            observation = await self._control(claim)
            if observation.state != ControlState.ACTIVE:
                return observation
            if not await asyncio.to_thread(self.authorizer.allowed, claim):
                return ExecutionObservation(ControlState.AUTHORIZATION_REVOKED)
            return observation

        try:
            # Driver/connection stalls cannot leave an adapter running past its lease.
            # The background DB thread may finish later; its write remains DB-fenced.
            return await asyncio.wait_for(check(), timeout=self.policy.lease_seconds / 3)
        except Exception:
            return ExecutionObservation(ControlState.LEASE_LOST)

    async def _monitor(self, claim):
        while True:
            await asyncio.sleep(self.policy.heartbeat_seconds)
            observation = await self._renew(claim)
            if observation.state != ControlState.ACTIVE:
                return observation

    async def _cleanup_heartbeat(self, claim):
        deadline = asyncio.get_running_loop().time() + self.policy.cancellation_grace_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                observation = await self._control(claim)
                if observation.state == ControlState.LEASE_LOST:
                    return
            except Exception:
                return
            await asyncio.sleep(min(self.policy.heartbeat_seconds, self.policy.cancellation_grace_seconds / 3))

    async def _cancel_owned(self, claim, execution):
        heartbeat = asyncio.create_task(self._cleanup_heartbeat(claim))
        try:
            try:
                await self._stop(execution)
            except WorkerExecutionUnstoppable:
                # Never acknowledge an unreaped runtime. Failure to record this
                # classification leaves the durable intent for expiry recovery.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.to_thread(self.store.task_control.abandon_cancel, claim.lease),
                                           self.policy.lease_seconds / 3)
                raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        await asyncio.wait_for(asyncio.to_thread(self.store.task_control.acknowledge_cancel, claim.lease),
                               self.policy.lease_seconds / 3)

    async def _cancel(self, claim, execution):
        # A repeated host cancellation cannot interrupt cleanup and create a
        # false acknowledgement or release a still-running adapter to new work.
        cleanup = asyncio.create_task(self._cancel_owned(claim, execution))
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError

    async def _finalize(self, claim, execution, *, status, result=None, error_code=None):
        try:
            outcome = await asyncio.to_thread(self.store.tasks.complete, claim.lease,
                                              status=status, result=result, error_code=error_code)
        except TaskValidationUnavailable:
            if status != "succeeded":
                raise
            # This typed failure is raised before the terminal write and the
            # repository transaction has rolled back. Do not repeat execution or
            # retry an uncertain write: settle failure under the SAME live fence.
            outcome = await asyncio.to_thread(self.store.tasks.complete, claim.lease,
                status="failed", error_code="TASK_VALIDATION_UNAVAILABLE")
        if outcome == CompletionOutcome.CANCELLATION_PENDING:
            await self._cancel(claim, execution)

    async def _handle_observation(self, claim, execution, observation):
        if observation.state == ControlState.CANCELLATION_REQUESTED:
            await self._cancel(claim, execution)
            return
        await self._stop(execution)
        if observation.state == ControlState.AUTHORIZATION_REVOKED:
            await self._finalize(claim, execution, status="failed", error_code="EXECUTION_NOT_AUTHORIZED")
        # A failed renewal must never write a late success/failure.

    async def run_once(self) -> bool:
        """Claim at most one task; True means claimed, not execution succeeded."""
        if self._poisoned or self._busy:
            raise RuntimeError("Worker is unavailable or already executing")
        self._busy = True
        execution = monitor = None
        try:
            rejected = await asyncio.to_thread(self.store.tasks.reject_disabled_project_tasks)
            await asyncio.to_thread(self.store.tasks.recover_expired)
            if rejected:
                return True
            claim = await asyncio.to_thread(self.store.tasks.claim, self.worker_id,
                                           lease_seconds=self.policy.lease_seconds, kinds=tuple(self.executors),
                                           workflow_agents=self.workflow_agents)
            if claim is None:
                return False
            observation = await self._renew(claim)
            if observation.state != ControlState.ACTIVE:
                await self._handle_observation(claim, None, observation)
                return True
            claim = claim.with_timeout(self.policy.execution_timeout_seconds)
            execution = asyncio.create_task(self._execute(claim))
            monitor = asyncio.create_task(self._monitor(claim))
            try:
                remaining = claim.limit_timeout(self.policy.execution_timeout_seconds)
            except ExecutionDeadlineExceeded:
                remaining = 0
            done, _ = await asyncio.wait({execution, monitor}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                try:
                    observation = monitor.result()
                except Exception:
                    observation = ExecutionObservation(ControlState.LEASE_LOST)
                await self._handle_observation(claim, execution, observation)
                return True
            if execution not in done:
                await self._stop(execution)
                await self._finalize(claim, execution, status="timed_out", error_code="EXECUTION_TIMEOUT_RESULT_UNKNOWN")
                return True
            try:
                result = execution.result()
            except WorkerExecutionUnstoppable:
                self._poisoned = True
                raise
            except TaskLeaseLost:
                # A nested executor lost its fence; only lease recovery may finalize it.
                return True
            except TaskCancellationRequested:
                await self._cancel(claim, execution)
            except ExecutionFailure as exc:
                await self._finalize(claim, execution, status=exc.status, error_code=exc.code)
            except (Exception, asyncio.CancelledError):
                await self._finalize(claim, execution, status="failed", error_code="EXECUTION_FAILED_RESULT_UNKNOWN")
            else:
                # Permission may shrink while the final model/tool call is returning.
                observation = await self._renew(claim)
                if observation.state == ControlState.ACTIVE:
                    await self._finalize(claim, execution, status="succeeded", result=result)
                else:
                    await self._handle_observation(claim, execution, observation)
            return True
        finally:
            try:
                if execution is not None and not self._poisoned:
                    await self._stop(execution)
            finally:
                if monitor is not None:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                self._busy = False

    async def run(self, stop: asyncio.Event):
        """Stop claiming on shutdown; allow the current bounded execution to finish."""
        while not stop.is_set():
            claimed = await self.run_once()
            if not claimed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.policy.poll_seconds)
                except TimeoutError:
                    pass
