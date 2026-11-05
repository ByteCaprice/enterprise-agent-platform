"""Independent deterministic Workflow executor with lease-fenced node checkpoints.

The runtime uses bounded pure-rule parallelism and serial registered IO adapters.
It never imports published Python targets or silently runs unsupported nodes.
Recovery creates a new pure-rule Task and may reuse verified rule checkpoints;
unknown IO is never replayed.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping

from domain.task import canonical_json
from domain.task_execution import (
    ExecutionFailure,
    ExecutionResourceUnstoppable,
    TaskCancellationRequested,
    TaskClaim,
    TaskLeaseLost,
)
from domain.workflow import WorkflowDefinition, WorkflowNode
from domain.workflow_runtime import WorkflowAdapterUnavailable, WorkflowNodeExecutor, WorkflowNodeRegistry
from framework.workflow import apply_rule, map_values, resolve_binding
from framework.workflow_plan import PlannedWorkflowNode, WorkflowExecutionPlan, WorkflowPlanError, compile_workflow
from orchestration.execution_authorization import ExecutionAuthorizer

NodeExecutor = WorkflowNodeExecutor


class WorkflowExecutor:
    def __init__(self, store, adapters: Mapping[str, NodeExecutor] | WorkflowNodeRegistry | None = None):
        self.store = store
        self.authorizer = ExecutionAuthorizer(store)
        self._registry = adapters if isinstance(adapters, WorkflowNodeRegistry) else WorkflowNodeRegistry.from_mapping(adapters or {})

    @property
    def adapters(self) -> Mapping[str, NodeExecutor]:
        return self._registry.executors

    @property
    def node_kinds(self) -> frozenset[str]:
        return self._registry.kinds | {"rule"}

    def _plan(self, claim: TaskClaim) -> WorkflowExecutionPlan:
        try:
            if claim.task.capability_kind != "workflow":
                raise ValueError("Invalid pinned workflow")
            claim.artifact.require_published(version=claim.task.version, content_hash=claim.task.content_hash)
            canonical_json(claim.artifact.definition)
            plan = compile_workflow(WorkflowDefinition.model_validate(claim.artifact.definition))
        except WorkflowPlanError as exc:
            raise ExecutionFailure(exc.code) from None
        except Exception:
            raise ExecutionFailure("WORKFLOW_DEFINITION_INVALID") from None
        # Availability is a deployment property, not a graph or authorization flag.
        try:
            self._registry.require(plan.required_adapters)
        except WorkflowAdapterUnavailable:
            raise ExecutionFailure("WORKFLOW_ADAPTER_UNAVAILABLE") from None
        return plan

    @staticmethod
    def _source_hash(claim, node, outputs):
        # Only referenced upstream outputs affect a checkpoint; unrelated siblings
        # must not invalidate it when bounded parallel scheduling is added later.
        bindings = list(node.input_mapping.values()) + ([node.when] if node.when is not None else [])
        referenced = {binding.node for binding in bindings if binding.source == "node"}
        upstream = {key: value for key, value in outputs.items() if key in referenced}
        source = {
            "task_input": hashlib.sha256(canonical_json(claim.input).encode()).hexdigest(),
            "upstream": hashlib.sha256(canonical_json(upstream).encode()).hexdigest(),
            "definition": claim.task.content_hash,
            "node": node.key,
        }
        return hashlib.sha256(canonical_json(source).encode()).hexdigest()

    @staticmethod
    def _evaluate(claim: TaskClaim, node: WorkflowNode, outputs: dict) -> tuple[str, dict | None]:
        condition = True if node.when is None else resolve_binding(node.when, claim.input, outputs)
        if type(condition) is not bool:
            raise ExecutionFailure("WORKFLOW_CONDITION_NOT_BOOLEAN")
        if not condition:
            return "skipped", None
        values = map_values(node.input_mapping, claim.input, outputs)
        if node.rule is None:
            raise ExecutionFailure("WORKFLOW_DEFINITION_INVALID")
        output = apply_rule(node.rule, values)
        # Bound the whole live workflow context, not just each individual node.
        canonical_json(outputs | {node.key: output})
        return "succeeded", output

    @staticmethod
    def _prepare(claim: TaskClaim, node: WorkflowNode, outputs: dict) -> dict | None:
        condition = True if node.when is None else resolve_binding(node.when, claim.input, outputs)
        if type(condition) is not bool:
            raise ExecutionFailure("WORKFLOW_CONDITION_NOT_BOOLEAN")
        return map_values(node.input_mapping, claim.input, outputs) if condition else None

    async def _perform(self, claim: TaskClaim, node: WorkflowNode, outputs: dict,
                       node_id: str) -> tuple[str, dict | None]:
        # CPU work must not starve the lease monitor. This thread only runs bounded,
        # pure JSON rules: after timeout it cannot call tools or mutate persistence.
        # wait_for bounds awaiting the result, not forcible termination of Python CPU.
        if node.kind == "rule":
            return await asyncio.to_thread(self._evaluate, claim, node, outputs)
        values = await asyncio.to_thread(self._prepare, claim, node, outputs)
        if values is None:
            return "skipped", None
        installed = self._registry.get(node.kind)
        if installed is None:
            raise ExecutionFailure("WORKFLOW_ADAPTER_UNAVAILABLE")
        output = await installed.adapter(claim, node, values, node_id)
        if not isinstance(output, dict):
            raise ExecutionFailure("WORKFLOW_NODE_OUTPUT_INVALID")
        await asyncio.to_thread(canonical_json, outputs | {node.key: output})
        return "succeeded", output

    async def _finish(self, claim, checkpoint, *, status, output=None, error_code=None):
        finished = await asyncio.to_thread(self.store.workflows.finish, claim.lease, checkpoint.record.id,
                                          status=status, output=output, error_code=error_code)
        if not finished:
            raise TaskLeaseLost("Workflow node no longer owns the execution lease")

    async def __call__(self, claim: TaskClaim) -> dict:
        plan = await asyncio.to_thread(self._plan, claim)
        outputs: dict[str, dict] = {}
        completed: set[str] = set()
        while len(completed) < len(plan.nodes):
            try:
                batch = plan.next_batch(completed)
            except WorkflowPlanError as exc:
                raise ExecutionFailure(exc.code) from None
            results = await asyncio.gather(*(self._run_planned_node(claim, node, outputs) for node in batch), return_exceptions=True)
            for node, output in zip(batch, results, strict=True):
                if isinstance(output, BaseException):
                    raise output
                if output is not None:
                    outputs[node.key] = output
                completed.add(node.key)
            await asyncio.to_thread(canonical_json, outputs)
        mapping = await asyncio.to_thread(plan.output_mapping)
        return await asyncio.to_thread(map_values, mapping, claim.input, outputs)

    async def _run_planned_node(self, claim: TaskClaim, planned: PlannedWorkflowNode, outputs: dict):
        # Handlers receive a detached definition, never the scheduler's graph state.
        node = await asyncio.to_thread(planned.materialize)
        return await self._run_node(claim, node, outputs)

    async def _run_node(self, claim, node, outputs):
        claim = claim.with_timeout(node.timeout_seconds)
        if not await asyncio.to_thread(self.authorizer.allowed, claim):
            raise ExecutionFailure("EXECUTION_NOT_AUTHORIZED")
        try:
            input_hash = await asyncio.to_thread(self._source_hash, claim, node, outputs)
            checkpoint = await asyncio.to_thread(self.store.workflows.begin, claim.lease, node.key, input_hash)
        except ValueError:
            raise ExecutionFailure("WORKFLOW_CHECKPOINT_CONFLICT") from None
        if checkpoint is None:
            raise TaskLeaseLost("Workflow node no longer owns the execution lease")
        if not checkpoint.created:
            if checkpoint.record.status == "succeeded" and checkpoint.output is not None:
                digest = hashlib.sha256(canonical_json(checkpoint.output).encode()).hexdigest()
                if digest != checkpoint.record.output_hash:
                    raise ExecutionFailure("WORKFLOW_CHECKPOINT_CORRUPT")
                canonical_json(outputs | {node.key: checkpoint.output})
            elif checkpoint.record.status != "skipped":
                raise ExecutionFailure("WORKFLOW_CHECKPOINT_CONFLICT")
            return checkpoint.output if checkpoint.record.status == "succeeded" else None
        try:
            status, output = await asyncio.wait_for(self._perform(claim, node, outputs, checkpoint.record.id),
                                                   timeout=claim.limit_timeout(node.timeout_seconds))
            claim.limit_timeout(node.timeout_seconds)
        except TimeoutError:
            await self._finish(claim, checkpoint, status="failed", error_code="WORKFLOW_NODE_TIMEOUT")
            raise ExecutionFailure("WORKFLOW_NODE_TIMEOUT", status="timed_out") from None
        except ExecutionFailure as exc:
            await self._finish(claim, checkpoint, status="failed", error_code=exc.code)
            raise
        except (TaskCancellationRequested, TaskLeaseLost, ExecutionResourceUnstoppable):
            raise
        except Exception:
            await self._finish(claim, checkpoint, status="failed", error_code="WORKFLOW_NODE_FAILED")
            raise ExecutionFailure("WORKFLOW_NODE_FAILED") from None
        await self._finish(claim, checkpoint, status=status, output=output)
        return output
