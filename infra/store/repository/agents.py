"""Root Agent evidence and atomic call/step barriers; never invokes a runtime or IO.

This is a persistence port, not a runtime or authorization service. The governed
broker/Gateways enforce live grants and validate model turns before acceptance.
"""

from __future__ import annotations

import hashlib
import re
from string import Template
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import select, text

from domain import LogEvent
from domain.capability import AgentTurnPolicyDefinition, PromptDefinition
from domain.platform_agent import (
    AgentCapabilityRef,
    AgentDefinition,
    AgentStorageConflict,
    AgentStorageError,
    AgentToolIntent,
    AgentValidationUnavailable,
)
from domain.platform_model import ModelUsage
from domain.platform_tool import ToolDefinition
from domain.schema_validation import JSONValidationError, JSONValidationUnavailable, ensure_self_contained_schema
from domain.task import canonical_json
from domain.task_execution import TaskLeaseLost
from infra.schema_validation import validate_json
from infra.store.model.ai_capability import AiCapability, AiCapabilityVersion
from infra.store.model.ai_model_provider import AiPlatformModelProvider
from infra.store.model.ai_platform_agent import AiAgentExecution, AiAgentStep
from infra.store.model.ai_platform_model_call import AiPlatformModelCall
from infra.store.model.ai_platform_tool import AiPlatformToolBinding, AiPlatformToolCall
from infra.store.model.ai_run_log import AiRunLog
from infra.store.model.ai_workflow_node import AiWorkflowNode
from infra.store.repository.base import BaseRepository
from infra.store.repository.execution import ExecutionWriteMode, active_execution, fence_execution_write
from infra.store.repository.models import ModelRepository
from infra.store.repository.task_events import append_task_event
from infra.store.repository.tools import ToolRepository


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _valid_hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AgentStorageError("Invalid evidence hash")


def _model_metadata(call, *, actual_model=None, usage=None, http_status=None, request_id=None):
    if actual_model is not None and (not isinstance(actual_model, str) or
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,199}", actual_model) is None):
        raise AgentStorageError("Invalid actual model")
    if usage is not None and not isinstance(usage, ModelUsage):
        raise AgentStorageError("Invalid model usage")
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        raise AgentStorageError("Invalid HTTP status")
    if request_id is not None and (not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", request_id) is None):
        raise AgentStorageError("Invalid request ID")
    call.actual_model, call.http_status, call.provider_request_id = actual_model, http_status, request_id
    if usage is not None:
        for key, value in usage.model_dump().items():
            setattr(call, key, value)


def _resource(session, project, reference, kind):
    found = session.execute(select(AiCapability, AiCapabilityVersion).join(
        AiCapabilityVersion, AiCapabilityVersion.capability_id == AiCapability.id,
    ).where(AiCapability.project_key == project, AiCapability.key == reference.key,
            AiCapability.kind == kind, AiCapabilityVersion.version == reference.version,
            AiCapabilityVersion.status == "published")).one_or_none()
    if found is None:
        raise AgentStorageError("Pinned resource unavailable")
    capability, artifact = found
    if artifact.to_domain().fingerprint() != artifact.content_hash:
        raise AgentStorageError("Pinned resource hash mismatch")
    return capability, artifact


def _validate(value, schema):
    canonical_json(value)
    try:
        ensure_self_contained_schema(schema)
        validate_json(value, schema)
    except JSONValidationUnavailable:
        raise AgentValidationUnavailable("JSON validation unavailable") from None
    except JSONValidationError:
        raise AgentStorageError("JSON validation failed") from None


class AgentRepository(BaseRepository):
    def check_ready(self):
        with self._read() as session:
            session.execute(select(AiAgentExecution.id).limit(0))
            session.execute(select(AiAgentStep.id).limit(0))

    @staticmethod
    def _audit(session, task, actor, event, execution_id):
        session.add(AiRunLog.from_domain(LogEvent(
            trace_id=f"task:{task.id}", component="agent_audit", event_type=event,
            message=event.replace("_", " "),
            data={"project_key": task.project_key, "task_id": task.id,
                  "execution_id": execution_id, "actor": actor},
        )))
        append_task_event(session, task.id, event, entity_kind="agent_execution", entity_id=execution_id)

    @staticmethod
    def _definition(session, task, *, parent_node_id=None):
        identity = session.get(AiCapability, task.capability_id)
        if identity is None or identity.project_key != task.project_key:
            raise AgentStorageError("Task capability unavailable")
        if parent_node_id is None:
            if identity.kind != "agent":
                raise AgentStorageError("Task is not a root Agent")
            reference = AgentCapabilityRef(key=identity.key, version=task.version)
        else:
            if identity.kind != "workflow":
                raise AgentStorageError("Embedded Agent requires a Workflow Task")
            _, workflow = _resource(session, task.project_key, AgentCapabilityRef(key=identity.key, version=task.version), "workflow")
            if workflow.content_hash != task.content_hash:
                raise AgentStorageError("Workflow snapshot changed")
            parent = session.scalar(select(AiWorkflowNode).where(
                AiWorkflowNode.id == parent_node_id, AiWorkflowNode.task_id == task.id,
            ).with_for_update())
            declared = next((node for node in workflow.definition.get("nodes", [])
                             if parent is not None and node.get("key") == parent.node_key), None)
            if parent is None or parent.kind != "agent" or declared is None or declared.get("kind") != "agent":
                raise AgentStorageError("Real Agent parent node unavailable")
            reference = AgentCapabilityRef.model_validate(declared.get("capability"))
            if reference.model_dump() not in workflow.dependencies:
                raise AgentStorageError("Workflow Agent dependency must be pinned")
        capability, artifact = _resource(session, task.project_key, reference, "agent")
        if parent_node_id is None and artifact.content_hash != task.content_hash:
            raise AgentStorageError("Agent does not match Task snapshot")
        definition = AgentDefinition.model_validate(artifact.definition)
        declared = {(ref["key"], ref["version"]) for ref in artifact.dependencies}
        if any((ref.key, ref.version) not in declared for ref in definition.dependency_refs()):
            raise AgentStorageError("Agent dependencies must be pinned")
        for reference, kind in [(definition.instruction_prompt, "prompt"), (definition.model_policy, "model_policy"),
                                *((tool.capability, "tool") for tool in definition.tools),
                                *((skill, "skill") for skill in definition.skills)]:
            _resource(session, task.project_key, reference, kind)
        return capability, artifact, definition

    def _evidence_scope(self, session, lease, execution_id, *, mode=ExecutionWriteMode.ADMIT):
        scope = active_execution(session, lease, mode=mode)
        if scope is None:
            raise TaskLeaseLost("Agent no longer owns its TaskAttempt")
        task, attempt = scope
        execution = session.scalar(select(AiAgentExecution).where(
            AiAgentExecution.id == execution_id, AiAgentExecution.project_key == task.project_key,
            AiAgentExecution.task_id == task.id, AiAgentExecution.task_attempt_id == attempt.id,
        ).with_for_update())
        if execution is None or execution.status != "running":
            raise AgentStorageConflict("Agent execution unavailable")
        if execution.parent_node_id is not None:
            parent = session.scalar(select(AiWorkflowNode.id).where(
                AiWorkflowNode.id == execution.parent_node_id, AiWorkflowNode.task_id == task.id,
                AiWorkflowNode.task_attempt_id == attempt.id, AiWorkflowNode.kind == "agent", AiWorkflowNode.status == "running",
            ))
            if parent is None:
                raise AgentStorageConflict("Agent parent node is not running")
        return task, execution

    def _scope(self, session, lease, execution_id):
        task, execution = self._evidence_scope(session, lease, execution_id)
        capability, artifact, definition = self._definition(session, task, parent_node_id=execution.parent_node_id)
        if (execution.agent_capability_id, execution.agent_key, execution.agent_version, execution.agent_hash) != (
            capability.id, capability.key, artifact.version, artifact.content_hash,
        ):
            raise AgentStorageError("Agent execution snapshot changed")
        return task, execution, artifact, definition

    @staticmethod
    def _steps(session, execution):
        return list(session.scalars(select(AiAgentStep).where(
            AiAgentStep.agent_execution_id == execution.id,
        ).order_by(AiAgentStep.turn, AiAgentStep.kind, AiAgentStep.slot, AiAgentStep.id).with_for_update()))

    def begin(self, lease, *, runtime_profile, parent_node_id=None, input_values=None):
        from domain.agent_runtime import AgentRuntimeDescriptor
        from domain.platform_agent import AgentCapacityUnavailable
        from infra.store.model.ai_operations import AiCapabilityConcurrency
        from infra.store.repository.limits import capability_running_count, concurrency_lock

        AgentRuntimeDescriptor(key="runtime", profile=runtime_profile)
        with self._sf.begin() as session:
            concurrency_lock(session)
            scope = active_execution(session, lease)
            if scope is None:
                raise TaskLeaseLost("Agent no longer owns its TaskAttempt")
            task, attempt = scope
            if parent_node_id is not None and session.scalar(select(AiWorkflowNode.id).where(
                AiWorkflowNode.id == parent_node_id, AiWorkflowNode.task_id == task.id,
                AiWorkflowNode.task_attempt_id == attempt.id, AiWorkflowNode.kind == "agent", AiWorkflowNode.status == "running",
            )) is None:
                raise AgentStorageError("Agent parent must be running in this Attempt")
            capability, artifact, definition = self._definition(session, task, parent_node_id=parent_node_id)
            if parent_node_id is not None:
                limit = session.get(AiCapabilityConcurrency, capability.id)
                if limit is not None and limit.max_running is not None:
                    root, embedded = capability_running_count(session, capability.id)
                    if root + embedded >= limit.max_running:
                        raise AgentCapacityUnavailable("Agent concurrency limit reached")
            if input_values is not None:
                _validate(input_values, artifact.input_schema)
            if session.scalar(select(AiAgentExecution.id).where(
                AiAgentExecution.task_attempt_id == attempt.id, AiAgentExecution.parent_node_id == parent_node_id,
            )):
                raise AgentStorageConflict("Agent execution cannot be replayed")
            _, prompt = _resource(session, task.project_key, definition.instruction_prompt, "prompt")
            template = Template(PromptDefinition.model_validate(prompt.definition).template)
            if not template.is_valid() or template.get_identifiers():
                raise AgentStorageError("Initial Agent instructions must use an empty variable set")
            _validate({}, prompt.input_schema)
            row = AiAgentExecution(
                id=uuid4().hex, project_key=task.project_key, task_id=task.id, task_attempt_id=attempt.id,
                agent_capability_id=capability.id, agent_key=capability.key, agent_version=artifact.version,
                agent_hash=artifact.content_hash, parent_node_id=parent_node_id,
                runtime_profile=runtime_profile,
                status="running", start_time=session.scalar(text("SELECT clock_timestamp()")),
            )
            session.add(row)
            self._audit(session, task, lease.worker_id, "agent_execution_started", row.id)
            fence_execution_write(session, lease)
            return row.to_record()

    def start_turn(self, lease, execution_id, *, input_hash):
        _valid_hash(input_hash)
        with self._sf.begin() as session:
            task, execution, _, definition = self._scope(session, lease, execution_id)
            steps = self._steps(session, execution)
            if any(step.status != "succeeded" for step in steps) or any(step.decision == "final" for step in steps):
                raise AgentStorageConflict("Previous Agent steps must complete before another model turn")
            turn = 1 + sum(step.kind == "model_turn" for step in steps)
            if turn > definition.limits.max_turns:
                raise AgentStorageConflict("Agent turn limit exceeded")
            cap, policy_artifact = _resource(session, task.project_key, definition.model_policy, "model_policy")
            policy = AgentTurnPolicyDefinition.model_validate(policy_artifact.definition)
            provider = session.get(AiPlatformModelProvider, (task.project_key, policy.provider_key))
            if (provider is None or not provider.enabled or policy.model not in provider.models or
                    policy.max_tokens > provider.max_tokens or
                    (policy.credential_ref is not None and policy.credential_ref != provider.credential_ref)):
                raise AgentStorageError("Agent requires one available catalog model")
            _, prompt = _resource(session, task.project_key, definition.instruction_prompt, "prompt")
            now = session.scalar(text("SELECT clock_timestamp()"))
            step = AiAgentStep(
                id=uuid4().hex, project_key=task.project_key, task_id=task.id, task_attempt_id=lease.attempt_id,
                agent_execution_id=execution.id, kind="model_turn", turn=turn, slot=0, status="running",
                resource_capability_id=cap.id, resource_key=cap.key, resource_version=policy_artifact.version,
                resource_hash=policy_artifact.content_hash, input_hash=input_hash, create_time=now, start_time=now,
            )
            session.add(step)
            session.flush()
            call = AiPlatformModelCall(
                id=uuid4().hex, project_key=task.project_key, task_id=task.id, task_attempt_id=lease.attempt_id,
                node_id=None, agent_execution_id=execution.id, agent_step_id=step.id, agent_step_kind="model_turn",
                policy_capability_id=cap.id, policy_key=cap.key, policy_version=policy_artifact.version,
                policy_hash=policy_artifact.content_hash, provider_key=provider.key, requested_model=policy.model,
                prompt_key=definition.instruction_prompt.key, prompt_version=prompt.version, prompt_hash=prompt.content_hash,
                status="running", input_hash=input_hash, start_time=now,
            )
            session.add(call)
            ModelRepository._audit(session, task.project_key, task.id, call.id, lease.worker_id, "model_call_started")
            fence_execution_write(session, lease)
            return step.to_record(), call.id

    def accept_turn(self, lease, execution_id, step_id, *, output_hash, actual_model,
                    final_result=None, intents=(), usage: ModelUsage | None = None, http_status=None, request_id=None):
        _valid_hash(output_hash)
        if not isinstance(actual_model, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,199}", actual_model) is None:
            raise AgentStorageError("Invalid actual model")
        if usage is not None and not isinstance(usage, ModelUsage):
            raise AgentStorageError("Invalid model usage")
        intents = TypeAdapter(list[AgentToolIntent]).validate_python(list(intents))
        if (final_result is not None) == bool(intents):
            raise AgentStorageError("Accept exactly a final result or a complete tool batch")
        with self._sf.begin() as session:
            task, execution, artifact, definition = self._scope(session, lease, execution_id)
            steps = self._steps(session, execution)
            step = next((item for item in steps if item.id == step_id and item.kind == "model_turn"), None)
            if step is None or step.status != "running":
                raise AgentStorageConflict("Model turn unavailable or already accepted")
            call = session.scalar(select(AiPlatformModelCall).where(
                AiPlatformModelCall.agent_step_id == step.id, AiPlatformModelCall.status == "running",
            ).with_for_update())
            if call is None:
                raise AgentStorageConflict("Model turn has no active call")
            pending = []
            now = session.scalar(text("SELECT clock_timestamp()"))
            if final_result is not None:
                if not isinstance(final_result, dict):
                    raise AgentStorageError("Final result must be an object")
                _validate(final_result, artifact.output_schema)
            else:
                if (step.turn >= definition.limits.max_turns or
                        sum(item.kind == "tool_call" for item in steps) + len(intents) > definition.limits.max_tool_calls or
                        len({item.source_call_id for item in intents}) != len(intents)):
                    raise AgentStorageConflict("Tool batch exceeds limits or repeats a source ID")
                aliases = {tool.alias: tool.capability for tool in definition.tools}
                for slot, intent in enumerate(intents):
                    reference = aliases.get(intent.alias)
                    if reference is None:
                        raise AgentStorageError("Unpublished tool alias")
                    cap, tool = _resource(session, task.project_key, reference, "tool")
                    ToolDefinition.model_validate(tool.definition)
                    _validate(intent.arguments, tool.input_schema)
                    pending.append(AiAgentStep(
                        id=uuid4().hex, project_key=task.project_key, task_id=task.id,
                        task_attempt_id=lease.attempt_id, agent_execution_id=execution.id, kind="tool_call",
                        turn=step.turn, slot=slot, status="pending", resource_capability_id=cap.id,
                        resource_key=cap.key, resource_version=tool.version, resource_hash=tool.content_hash,
                        source_model_step_id=step.id, source_model_kind="model_turn", dispatch_id=uuid4().hex,
                        tool_alias=intent.alias, source_call_id_hash=_hash(intent.source_call_id),
                        input_hash=_hash(intent.arguments), create_time=now,
                    ))
            _model_metadata(call, actual_model=actual_model, usage=usage, http_status=http_status, request_id=request_id)
            call.status, call.output_hash, call.finish_time = "succeeded", output_hash, now
            step.status, step.output_hash, step.finish_time = "succeeded", output_hash, now
            step.decision = "final" if final_result is not None else "tools"
            step.final_result_hash = _hash(final_result) if final_result is not None else None
            session.add_all(pending)
            ModelRepository._audit(session, task.project_key, task.id, call.id, lease.worker_id, "model_call_succeeded")
            self._audit(session, task, lease.worker_id, "agent_turn_accepted", execution.id)
            fence_execution_write(session, lease)
            return [item.to_record() for item in pending]

    def reject_call(self, lease, execution_id, step_id, *, error_code, cancelled=False,
                    actual_model=None, usage=None, http_status=None, request_id=None):
        """Retain known response Usage even when policy/turn delivery is rejected."""
        if not isinstance(error_code, str) or re.fullmatch(r"(?:MODEL|TOOL)_[A-Z0-9_]{1,90}", error_code) is None:
            raise AgentStorageError("Invalid call failure code")
        with self._sf.begin() as session:
            task, execution = self._evidence_scope(session, lease, execution_id, mode=ExecutionWriteMode.SETTLE)
            step = next((item for item in self._steps(session, execution) if item.id == step_id), None)
            if step is None or step.status != "running":
                raise AgentStorageConflict("Call step is not running")
            is_model = step.kind == "model_turn"
            if not error_code.startswith("MODEL_" if is_model else "TOOL_"):
                raise AgentStorageError("Failure code does not match its call kind")
            entity = AiPlatformModelCall if is_model else AiPlatformToolCall
            call = session.scalar(select(entity).where(entity.agent_step_id == step.id, entity.status == "running").with_for_update())
            if call is None:
                raise AgentStorageConflict("Step has no active call")
            if is_model:
                _model_metadata(call, actual_model=actual_model, usage=usage, http_status=http_status, request_id=request_id)
            now = session.scalar(text("SELECT clock_timestamp()"))
            call.status, call.error_code, call.finish_time = "cancelled" if cancelled else "failed", error_code, now
            step.status, step.error_code, step.finish_time = call.status, "AGENT_CALL_REJECTED_RESULT_UNKNOWN", now
            if is_model:
                ModelRepository._audit(session, task.project_key, task.id, call.id, lease.worker_id, "model_call_" + call.status)
            else:
                ToolRepository._audit(session, task.project_key, lease.worker_id, "tool_call_" + call.status,
                                      task_id=task.id, call_id=call.id)
            fence_execution_write(session, lease, mode=ExecutionWriteMode.SETTLE)

    def start_tool(self, lease, execution_id, dispatch_id, *, arguments):
        with self._sf.begin() as session:
            task, execution, _, definition = self._scope(session, lease, execution_id)
            steps = self._steps(session, execution)
            step = next((item for item in steps if item.dispatch_id == dispatch_id), None)
            if step is None or step.status != "pending":
                raise AgentStorageConflict("Tool dispatch unavailable or already started")
            if any(item.status != "succeeded" for item in steps
                   if (item.turn, item.kind, item.slot) < (step.turn, step.kind, step.slot)):
                raise AgentStorageConflict("Agent tools must execute in accepted source order")
            if _hash(arguments) != step.input_hash:
                raise AgentStorageConflict("Tool arguments do not match the accepted intent")
            reference = next(tool.capability for tool in definition.tools if tool.alias == step.tool_alias)
            cap, artifact = _resource(session, task.project_key, reference, "tool")
            if artifact.content_hash != step.resource_hash:
                raise AgentStorageError("Accepted tool snapshot changed")
            tool = ToolDefinition.model_validate(artifact.definition)
            _validate(arguments, artifact.input_schema)
            binding = session.get(AiPlatformToolBinding, (task.project_key, tool.binding_key))
            if binding is None or not binding.enabled or tool.timeout_seconds > binding.max_timeout_seconds:
                raise AgentStorageError("Tool binding unavailable")
            now = session.scalar(text("SELECT clock_timestamp()"))
            call = AiPlatformToolCall(
                id=uuid4().hex, project_key=task.project_key, task_id=task.id, task_attempt_id=lease.attempt_id,
                node_id=None, agent_execution_id=execution.id, agent_step_id=step.id, agent_step_kind="tool_call",
                tool_capability_id=cap.id, tool_key=cap.key, tool_version=artifact.version,
                tool_hash=artifact.content_hash, binding_key=binding.key, handler_key=binding.handler_key,
                handler_version=binding.handler_version, operation_type="read_only", status="running",
                input_hash=step.input_hash, start_time=now,
            )
            session.add(call)
            step.status, step.start_time = "running", now
            ToolRepository._audit(session, task.project_key, lease.worker_id, "tool_call_started",
                                  task_id=task.id, call_id=call.id, binding_key=binding.key)
            fence_execution_write(session, lease)
            return step.to_record(), call.id

    def finish_tool(self, lease, execution_id, step_id, *, output):
        with self._sf.begin() as session:
            task, execution, _, definition = self._scope(session, lease, execution_id)
            step = next((item for item in self._steps(session, execution) if item.id == step_id), None)
            if step is None or step.kind != "tool_call" or step.status != "running":
                raise AgentStorageConflict("Tool step is not running")
            reference = next(tool.capability for tool in definition.tools if tool.alias == step.tool_alias)
            _, artifact = _resource(session, task.project_key, reference, "tool")
            if not isinstance(output, dict):
                raise AgentStorageError("Tool output must be an object")
            _validate(output, artifact.output_schema)
            call = session.scalar(select(AiPlatformToolCall).where(
                AiPlatformToolCall.agent_step_id == step.id, AiPlatformToolCall.status == "running",
            ).with_for_update())
            if call is None:
                raise AgentStorageConflict("Tool step has no active call")
            now = session.scalar(text("SELECT clock_timestamp()"))
            call.status, call.output_hash, call.finish_time = "succeeded", _hash(output), now
            step.status, step.output_hash, step.finish_time = "succeeded", call.output_hash, now
            ToolRepository._audit(session, task.project_key, lease.worker_id, "tool_call_succeeded",
                                  task_id=task.id, call_id=call.id)
            fence_execution_write(session, lease)

    def finish(self, lease, execution_id, *, result):
        with self._sf.begin() as session:
            task, execution, artifact, _ = self._scope(session, lease, execution_id)
            steps = self._steps(session, execution)
            finals = [item for item in steps if item.decision == "final"]
            if not steps or any(item.status != "succeeded" for item in steps) or len(finals) != 1:
                raise AgentStorageConflict("Agent cannot finish before all steps and a final turn complete")
            for model in (AiPlatformModelCall, AiPlatformToolCall):
                if session.scalar(select(model.id).where(
                    model.agent_execution_id == execution.id, model.status == "running",
                ).limit(1)) is not None:
                    raise AgentStorageConflict("Agent cannot finish with an active call")
            _validate(result, artifact.output_schema)
            if not isinstance(result, dict) or finals[0].final_result_hash != _hash(result):
                raise AgentStorageConflict("Agent result does not match its accepted final turn")
            now = session.scalar(text("SELECT clock_timestamp()"))
            execution.status, execution.output_hash, execution.finish_time = "succeeded", _hash(result), now
            self._audit(session, task, lease.worker_id, "agent_execution_succeeded", execution.id)
            fence_execution_write(session, lease)
