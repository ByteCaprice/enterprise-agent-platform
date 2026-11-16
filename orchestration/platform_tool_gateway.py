"""Governed, non-retrying read-only tool invocation on the shared Task lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass

from domain.agent_invocation import AgentInvocation
from domain.capability import CapabilityDependency, CapabilityKind, CapabilityVersionRecord
from domain.execution_context import ExecutionDeadlineExceeded
from domain.execution_ports import AgentToolLedger, ExecutionResources, ToolCallLedger
from domain.platform_agent import AgentStorageConflict, AgentStorageError, AgentValidationUnavailable
from domain.platform_tool import ToolBindingRecord, ToolConfigurationError, ToolConflict, ToolContext, ToolDefinition
from domain.schema_validation import JSONValidationUnavailable
from domain.task import canonical_json
from domain.task_execution import (
    ExecutionFailure,
    ExecutionResourceUnstoppable,
    TaskCancellationRequested,
    TaskClaim,
    TaskLeaseLost,
)
from framework.readonly_tools import ReadonlyToolRegistry
from orchestration.execution_async import accounting_error, run_sync
from orchestration.published_capabilities import PublishedCapabilities


@dataclass(frozen=True)
class ResolvedTool:
    artifact: CapabilityVersionRecord
    definition: ToolDefinition
    binding: ToolBindingRecord


class PlatformToolGateway:
    def __init__(self, resources: ExecutionResources, calls: ToolCallLedger,
                 agent_calls: AgentToolLedger, registry: ReadonlyToolRegistry):
        self.resources = resources
        self.calls = calls
        self.agent_calls = agent_calls
        self.registry = registry
        self.authorizer = resources.authorization
        self.catalog = PublishedCapabilities(resources.capabilities)

    def resolve(self, claim: TaskClaim | AgentInvocation, reference: CapabilityDependency) -> ResolvedTool:
        try:
            if not (self.authorizer.allowed(claim) and
                    self.authorizer.allowed_resource(claim, "tool:invoke", reference.key)):
                raise ExecutionFailure("TOOL_ACCESS_DENIED")
            if (isinstance(claim, AgentInvocation) and claim.parent_node_id is not None and
                    not self.authorizer.allowed_resource(claim, "agent:invoke", claim.reference.key)):
                raise ExecutionFailure("TOOL_AGENT_ACCESS_DENIED")
            artifact = self.catalog.resolve(claim.task.project_key, reference, CapabilityKind.TOOL)
            definition = ToolDefinition.model_validate(artifact.definition)
            binding = self.calls.binding(claim.task.project_key, definition.binding_key)
            if not binding.enabled:
                raise ExecutionFailure("TOOL_BINDING_DISABLED")
            if definition.timeout_seconds > binding.max_timeout_seconds:
                raise ExecutionFailure("TOOL_POLICY_DENIED")
            handler = self.registry.resolve(binding.handler_key, binding.handler_version)
            if handler is None:
                raise ExecutionFailure("TOOL_HANDLER_UNAVAILABLE")
            if definition.implementation is not None and (
                (binding.handler_key, binding.handler_version) != (definition.implementation.key, definition.implementation.version)
                or handler.implementation != definition.implementation
            ):
                raise ExecutionFailure("TOOL_IMPLEMENTATION_MISMATCH")
            return ResolvedTool(artifact, definition, binding)
        except ExecutionFailure:
            raise
        except Exception:
            raise ExecutionFailure("TOOL_CONFIGURATION_UNAVAILABLE") from None

    def _validate(self, value, schema, code):
        try:
            if not isinstance(value, dict):
                raise ValueError
            canonical_json(value)
            self.resources.validation.validate(value, schema)
        except JSONValidationUnavailable:
            raise ExecutionFailure("TOOL_VALIDATION_UNAVAILABLE") from None
        except Exception:
            raise ExecutionFailure(code) from None

    async def _finish(self, claim, call_id, *, status, output_hash=None, error_code=None):
        try:
            finished = await run_sync(self.calls.finish_call, claim.lease, call_id,
                                      status=status, output_hash=output_hash, error_code=error_code,
                                      _on_cancel_error=accounting_error("TOOL_ACCOUNTING_RESULT_UNKNOWN"))
        except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable, ExecutionFailure):
            raise
        except Exception:
            raise ExecutionFailure("TOOL_ACCOUNTING_RESULT_UNKNOWN") from None
        if not finished:
            raise TaskLeaseLost("Tool call no longer owns its execution lease")

    async def invoke(self, claim: TaskClaim, node_id: str, reference: CapabilityDependency, values: dict) -> dict:
        resolved = await run_sync(self.resolve, claim, reference)
        try:
            claim.limit_timeout(resolved.definition.timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("TOOL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
        await run_sync(self._validate, values, resolved.artifact.input_schema, "TOOL_INPUT_INVALID")
        encoded = await run_sync(canonical_json, values)
        input_hash = hashlib.sha256(encoded.encode()).hexdigest()
        arguments = await run_sync(deepcopy, values)
        handler = self.registry.resolve(resolved.binding.handler_key, resolved.binding.handler_version)
        if handler is None:
            raise ExecutionFailure("TOOL_HANDLER_UNAVAILABLE")
        try:
            claim.limit_timeout(resolved.definition.timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("TOOL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
        try:
            call_id = await run_sync(
                self.calls.start_call, claim.lease, node_id, tool_key=reference.key,
                tool_version=reference.version, tool_hash=resolved.artifact.content_hash,
                binding_key=resolved.binding.key, handler_key=resolved.binding.handler_key,
                handler_version=resolved.binding.handler_version, input_hash=input_hash,
                _on_cancel_error=accounting_error("TOOL_ACCOUNTING_RESULT_UNKNOWN"),
            )
        except ToolConflict:
            raise ExecutionFailure("TOOL_CALL_CONFLICT") from None
        except ToolConfigurationError:
            raise ExecutionFailure("TOOL_CONFIGURATION_UNAVAILABLE") from None
        except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable, ExecutionFailure):
            raise
        except Exception:
            raise ExecutionFailure("TOOL_ACCOUNTING_RESULT_UNKNOWN") from None
        if call_id is None:
            raise TaskLeaseLost("Tool call no longer owns its execution lease")
        context = ToolContext(claim.task.project_key, claim.task.submitted_by, claim.task.id,
                              claim.lease.attempt_id, node_id, reference.key, reference.version)

        async def finish(*, status, output_hash=None, error_code=None, output=None):
            await self._finish(claim, call_id, status=status, output_hash=output_hash, error_code=error_code)

        return await self._execute(claim, reference, arguments, resolved, handler, context, finish)

    async def invoke_agent(self, claim: AgentInvocation, execution_id: str, dispatch_id: str,
                           reference: CapabilityDependency, values: dict) -> dict:
        resolved = await run_sync(self.resolve, claim, reference)
        try:
            claim.limit_timeout(resolved.definition.timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("TOOL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
        await run_sync(self._validate, values, resolved.artifact.input_schema, "TOOL_INPUT_INVALID")
        arguments = await run_sync(deepcopy, values)
        handler = self.registry.resolve(resolved.binding.handler_key, resolved.binding.handler_version)
        if handler is None:
            raise ExecutionFailure("TOOL_HANDLER_UNAVAILABLE")
        try:
            claim.limit_timeout(resolved.definition.timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("TOOL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
        try:
            step, _ = await run_sync(self.agent_calls.start_tool, claim.lease, execution_id, dispatch_id,
                                     arguments=arguments,
                                     _on_cancel_error=accounting_error("TOOL_ACCOUNTING_RESULT_UNKNOWN"))
        except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable, ExecutionFailure):
            raise
        except AgentStorageConflict:
            raise ExecutionFailure("TOOL_CALL_CONFLICT") from None
        except AgentValidationUnavailable:
            raise ExecutionFailure("TOOL_VALIDATION_UNAVAILABLE") from None
        except AgentStorageError:
            raise ExecutionFailure("TOOL_CONFIGURATION_UNAVAILABLE") from None
        except Exception:
            raise ExecutionFailure("TOOL_ACCOUNTING_RESULT_UNKNOWN") from None

        async def finish(*, status, output_hash=None, error_code=None, output=None):
            try:
                if status == "succeeded":
                    await run_sync(self.agent_calls.finish_tool, claim.lease, execution_id, step.id, output=output,
                                   _on_cancel_error=accounting_error("TOOL_ACCOUNTING_RESULT_UNKNOWN"))
                else:
                    await run_sync(self.agent_calls.reject_call, claim.lease, execution_id, step.id,
                                   error_code=error_code, cancelled=status == "cancelled",
                                   _on_cancel_error=accounting_error("TOOL_ACCOUNTING_RESULT_UNKNOWN"))
            except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable, ExecutionFailure):
                raise
            except AgentValidationUnavailable:
                raise ExecutionFailure("TOOL_VALIDATION_UNAVAILABLE") from None
            except AgentStorageError:
                raise ExecutionFailure("TOOL_ACCOUNTING_RESULT_UNKNOWN") from None
            except Exception:
                raise ExecutionFailure("TOOL_ACCOUNTING_RESULT_UNKNOWN") from None

        if (step.resource_key, step.resource_version, step.resource_hash) != (
            reference.key, reference.version, resolved.artifact.content_hash,
        ):
            await finish(status="failed", error_code="TOOL_CONFIGURATION_CHANGED")
            raise ExecutionFailure("TOOL_CONFIGURATION_CHANGED")
        context = ToolContext(claim.task.project_key, claim.task.submitted_by, claim.task.id,
                              claim.lease.attempt_id, None, reference.key, reference.version, execution_id, step.id)
        return await self._execute(claim, reference, arguments, resolved, handler, context, finish)

    async def _execute(self, claim, reference, arguments, resolved, handler, context, finish):
        try:
            try:
                timeout_seconds = claim.limit_timeout(resolved.definition.timeout_seconds)
            except ExecutionDeadlineExceeded:
                raise ExecutionFailure("TOOL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            async with asyncio.timeout(timeout_seconds):
                output = await handler.execute(context, arguments)
            pending = asyncio.current_task()
            if pending is not None and pending.cancelling():
                raise asyncio.CancelledError
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            current = await run_sync(self.resolve, claim, reference)
            if current.artifact.content_hash != resolved.artifact.content_hash or current.binding != resolved.binding:
                raise ExecutionFailure("TOOL_CONFIGURATION_CHANGED")
            await run_sync(self._validate, output, resolved.artifact.output_schema, "TOOL_OUTPUT_INVALID")
            output_hash = hashlib.sha256((await run_sync(canonical_json, output)).encode()).hexdigest()
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            claim.limit_timeout(resolved.definition.timeout_seconds)
        except asyncio.CancelledError:
            await finish(status="cancelled", error_code="TOOL_CANCELLED_RESULT_UNKNOWN")
            raise
        except TimeoutError:
            try:
                claim.limit_timeout(resolved.definition.timeout_seconds)
            except ExecutionDeadlineExceeded:
                code = "TOOL_EXECUTION_DEADLINE_EXCEEDED"
                await finish(status="failed", error_code=code)
                raise ExecutionFailure(code, status="timed_out") from None
            await finish(status="failed", error_code="TOOL_TIMEOUT_RESULT_UNKNOWN")
            raise ExecutionFailure("TOOL_TIMEOUT_RESULT_UNKNOWN", status="timed_out") from None
        except (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable):
            raise
        except ExecutionFailure as exc:
            if exc.code.endswith("ACCOUNTING_RESULT_UNKNOWN"):
                raise
            code = exc.code if re.fullmatch(r"TOOL_[A-Z0-9_]{1,90}", exc.code) else "TOOL_EXECUTION_FAILED_RESULT_UNKNOWN"
            await finish(status="failed", error_code=code)
            raise ExecutionFailure(code, status=exc.status) from None
        except Exception:
            await finish(status="failed", error_code="TOOL_EXECUTION_FAILED_RESULT_UNKNOWN")
            raise ExecutionFailure("TOOL_EXECUTION_FAILED_RESULT_UNKNOWN") from None
        await finish(status="succeeded", output_hash=output_hash, output=output)
        return output
