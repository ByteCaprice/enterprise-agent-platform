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
from domain.platform_agent import AgentStorageConflict, AgentStorageError, AgentValidationUnavailable
from domain.platform_tool import ToolBindingRecord, ToolConfigurationError, ToolConflict, ToolContext, ToolDefinition
from domain.schema_validation import JSONValidationUnavailable
from domain.task import canonical_json
from domain.task_execution import ExecutionFailure, TaskCancellationRequested, TaskClaim, TaskLeaseLost
from framework.readonly_tools import ReadonlyToolRegistry
from infra.schema_validation import validate_json
from orchestration.execution_authorization import ExecutionAuthorizer
from orchestration.published_capabilities import PublishedCapabilities


@dataclass(frozen=True)
class ResolvedTool:
    artifact: CapabilityVersionRecord
    definition: ToolDefinition
    binding: ToolBindingRecord


class PlatformToolGateway:
    def __init__(self, store, registry: ReadonlyToolRegistry):
        self.store = store
        self.registry = registry
        self.authorizer = ExecutionAuthorizer(store)
        self.catalog = PublishedCapabilities(store.capabilities)

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
            binding = self.store.tool_bindings.binding(claim.task.project_key, definition.binding_key)
            if not binding.enabled:
                raise ExecutionFailure("TOOL_BINDING_DISABLED")
            if definition.timeout_seconds > binding.max_timeout_seconds:
                raise ExecutionFailure("TOOL_POLICY_DENIED")
            if self.registry.resolve(binding.handler_key, binding.handler_version) is None:
                raise ExecutionFailure("TOOL_HANDLER_UNAVAILABLE")
            return ResolvedTool(artifact, definition, binding)
        except ExecutionFailure:
            raise
        except Exception:
            raise ExecutionFailure("TOOL_CONFIGURATION_UNAVAILABLE") from None

    @staticmethod
    def _validate(value, schema, code):
        try:
            if not isinstance(value, dict):
                raise ValueError
            canonical_json(value)
            validate_json(value, schema)
        except JSONValidationUnavailable:
            raise ExecutionFailure("TOOL_VALIDATION_UNAVAILABLE") from None
        except Exception:
            raise ExecutionFailure(code) from None

    async def _finish(self, claim, call_id, *, status, output_hash=None, error_code=None):
        try:
            finished = await asyncio.to_thread(self.store.tool_bindings.finish_call, claim.lease, call_id,
                                              status=status, output_hash=output_hash, error_code=error_code)
        except TaskCancellationRequested:
            raise
        except Exception:
            raise ExecutionFailure("TOOL_ACCOUNTING_RESULT_UNKNOWN") from None
        if not finished:
            raise TaskLeaseLost("Tool call no longer owns its execution lease")

    async def invoke(self, claim: TaskClaim, node_id: str, reference: CapabilityDependency, values: dict) -> dict:
        resolved = await asyncio.to_thread(self.resolve, claim, reference)
        try:
            claim.limit_timeout(resolved.definition.timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("TOOL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
        await asyncio.to_thread(self._validate, values, resolved.artifact.input_schema, "TOOL_INPUT_INVALID")
        encoded = await asyncio.to_thread(canonical_json, values)
        input_hash = hashlib.sha256(encoded.encode()).hexdigest()
        arguments = await asyncio.to_thread(deepcopy, values)
        handler = self.registry.resolve(resolved.binding.handler_key, resolved.binding.handler_version)
        if handler is None:
            raise ExecutionFailure("TOOL_HANDLER_UNAVAILABLE")
        try:
            claim.limit_timeout(resolved.definition.timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("TOOL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
        try:
            call_id = await asyncio.to_thread(
                self.store.tool_bindings.start_call, claim.lease, node_id, tool_key=reference.key,
                tool_version=reference.version, tool_hash=resolved.artifact.content_hash,
                binding_key=resolved.binding.key, handler_key=resolved.binding.handler_key,
                handler_version=resolved.binding.handler_version, input_hash=input_hash,
            )
        except ToolConflict:
            raise ExecutionFailure("TOOL_CALL_CONFLICT") from None
        except ToolConfigurationError:
            raise ExecutionFailure("TOOL_CONFIGURATION_UNAVAILABLE") from None
        except TaskCancellationRequested:
            raise
        except Exception:
            raise ExecutionFailure("TOOL_ACCOUNTING_UNAVAILABLE") from None
        if call_id is None:
            raise TaskLeaseLost("Tool call no longer owns its execution lease")
        context = ToolContext(claim.task.project_key, claim.task.submitted_by, claim.task.id,
                              claim.lease.attempt_id, node_id, reference.key, reference.version)

        async def finish(*, status, output_hash=None, error_code=None, output=None):
            await self._finish(claim, call_id, status=status, output_hash=output_hash, error_code=error_code)

        return await self._execute(claim, reference, arguments, resolved, handler, context, finish)

    async def invoke_agent(self, claim: AgentInvocation, execution_id: str, dispatch_id: str,
                           reference: CapabilityDependency, values: dict) -> dict:
        resolved = await asyncio.to_thread(self.resolve, claim, reference)
        try:
            claim.limit_timeout(resolved.definition.timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("TOOL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
        await asyncio.to_thread(self._validate, values, resolved.artifact.input_schema, "TOOL_INPUT_INVALID")
        arguments = await asyncio.to_thread(deepcopy, values)
        handler = self.registry.resolve(resolved.binding.handler_key, resolved.binding.handler_version)
        if handler is None:
            raise ExecutionFailure("TOOL_HANDLER_UNAVAILABLE")
        try:
            claim.limit_timeout(resolved.definition.timeout_seconds)
        except ExecutionDeadlineExceeded:
            raise ExecutionFailure("TOOL_EXECUTION_DEADLINE_EXCEEDED", status="timed_out") from None
        try:
            step, _ = await asyncio.to_thread(self.store.agents.start_tool, claim.lease, execution_id, dispatch_id,
                                             arguments=arguments)
        except (TaskLeaseLost, TaskCancellationRequested):
            raise
        except AgentStorageConflict:
            raise ExecutionFailure("TOOL_CALL_CONFLICT") from None
        except AgentValidationUnavailable:
            raise ExecutionFailure("TOOL_VALIDATION_UNAVAILABLE") from None
        except AgentStorageError:
            raise ExecutionFailure("TOOL_CONFIGURATION_UNAVAILABLE") from None
        except Exception:
            raise ExecutionFailure("TOOL_ACCOUNTING_UNAVAILABLE") from None

        async def finish(*, status, output_hash=None, error_code=None, output=None):
            try:
                if status == "succeeded":
                    await asyncio.to_thread(self.store.agents.finish_tool, claim.lease, execution_id, step.id, output=output)
                else:
                    await asyncio.to_thread(self.store.agents.reject_call, claim.lease, execution_id, step.id,
                                           error_code=error_code, cancelled=status == "cancelled")
            except TaskCancellationRequested:
                if status == "succeeded":
                    await asyncio.to_thread(self.store.agents.reject_call, claim.lease, execution_id, step.id,
                                           error_code="TOOL_CANCELLED_RESPONSE_DISCARDED", cancelled=True)
                raise
            except TaskLeaseLost:
                raise
            except AgentValidationUnavailable:
                if status == "succeeded":
                    try:
                        await asyncio.to_thread(self.store.agents.reject_call, claim.lease, execution_id, step.id,
                                                error_code="TOOL_VALIDATION_UNAVAILABLE")
                    except Exception:
                        raise ExecutionFailure("TOOL_ACCOUNTING_RESULT_UNKNOWN") from None
                    raise ExecutionFailure("TOOL_VALIDATION_UNAVAILABLE") from None
                raise ExecutionFailure("TOOL_ACCOUNTING_RESULT_UNKNOWN") from None
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
            current = await asyncio.to_thread(self.resolve, claim, reference)
            if current.artifact.content_hash != resolved.artifact.content_hash or current.binding != resolved.binding:
                raise ExecutionFailure("TOOL_CONFIGURATION_CHANGED")
            await asyncio.to_thread(self._validate, output, resolved.artifact.output_schema, "TOOL_OUTPUT_INVALID")
            output_hash = hashlib.sha256((await asyncio.to_thread(canonical_json, output)).encode()).hexdigest()
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
        except TaskCancellationRequested:
            raise
        except ExecutionFailure as exc:
            code = exc.code if re.fullmatch(r"TOOL_[A-Z0-9_]{1,90}", exc.code) else "TOOL_EXECUTION_FAILED_RESULT_UNKNOWN"
            await finish(status="failed", error_code=code)
            raise ExecutionFailure(code, status=exc.status) from None
        except Exception:
            await finish(status="failed", error_code="TOOL_EXECUTION_FAILED_RESULT_UNKNOWN")
            raise ExecutionFailure("TOOL_EXECUTION_FAILED_RESULT_UNKNOWN") from None
        await finish(status="succeeded", output_hash=output_hash, output=output)
        return output
