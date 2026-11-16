"""Immutable tool bindings and node-scoped, lease-fenced invocation evidence."""

from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from domain import utc_now
from domain.platform_tool import ToolBindingNotFound, ToolConfigurationError, ToolConflict, ToolDefinition
from domain.task import TaskNotFound
from domain.task_execution import TaskCancellationRequested
from infra.store.model import (
    AiCapability,
    AiCapabilityVersion,
    AiPlatformToolBinding,
    AiPlatformToolCall,
    AiProject,
    AiTask,
    AiWorkflowNode,
)
from infra.store.repository.base import BaseRepository
from infra.store.repository.execution import ExecutionWriteMode, active_execution
from infra.store.repository.execution_effects import record_tool_audit, record_tool_call_transition
from infra.store.repository.workflow_scope import declared_workflow_activity

_HASH = re.compile(r"^[0-9a-f]{64}$")
_ERROR = re.compile(r"^TOOL_[A-Z0-9_]{1,90}$")


class ToolRepository(BaseRepository):
    def check_ready(self):
        with self._read() as session:
            session.execute(select(AiPlatformToolBinding).limit(0))
            session.execute(select(AiPlatformToolCall).limit(0))

    def create_binding(self, project_key, spec, actor):
        with self._sf.begin() as session:
            project = session.scalar(select(AiProject).where(AiProject.key == project_key).with_for_update())
            if project is None or not project.enabled:
                raise ToolBindingNotFound("Project is unavailable")
            if session.get(AiPlatformToolBinding, (project_key, spec.key)) is not None:
                raise ToolConflict("Tool binding already exists")
            row = AiPlatformToolBinding(project_key=project_key, **spec.model_dump(), enabled=True,
                                        created_by=actor, create_time=utc_now())
            session.add(row)
            record_tool_audit(session, project_key, actor, "tool_binding_created", binding_key=spec.key)
            session.flush()
            return row.to_record()

    def binding(self, project_key, key):
        with self._read() as session:
            row = session.get(AiPlatformToolBinding, (project_key, key))
            if row is None:
                raise ToolBindingNotFound("Tool binding not found")
            return row.to_record()

    def list_bindings(self, project_key, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Invalid limit")
        with self._read() as session:
            rows = session.scalars(select(AiPlatformToolBinding).where(
                AiPlatformToolBinding.project_key == project_key,
            ).order_by(AiPlatformToolBinding.key).limit(limit))
            return [row.to_record() for row in rows]

    def set_binding_enabled(self, project_key, key, enabled, actor):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        with self._sf.begin() as session:
            row = session.scalar(select(AiPlatformToolBinding).where(
                AiPlatformToolBinding.project_key == project_key, AiPlatformToolBinding.key == key,
            ).with_for_update())
            if row is None:
                raise ToolBindingNotFound("Tool binding not found")
            row.enabled = enabled
            record_tool_audit(session, project_key, actor, "tool_binding_enabled" if enabled else "tool_binding_disabled",
                        binding_key=key)
            session.flush()
            return row.to_record()

    def start_call(self, lease, node_id, *, tool_key, tool_version, tool_hash, binding_key,
                   handler_key, handler_version, input_hash):
        if not all(isinstance(value, str) and _HASH.fullmatch(value) for value in (tool_hash, input_hash)):
            raise ToolConfigurationError("Invalid tool call hashes")
        with self._sf.begin() as session:
            scope = active_execution(session, lease)
            if scope is None:
                return None
            task, attempt = scope
            node = session.scalar(select(AiWorkflowNode).where(
                AiWorkflowNode.id == node_id, AiWorkflowNode.task_id == task.id,
                AiWorkflowNode.task_attempt_id == attempt.id,
            ).with_for_update())
            if node is None or node.kind != "tool" or node.status != "running":
                raise ToolConfigurationError("Tool node is unavailable")
            try:
                declared, _, reference = declared_workflow_activity(session, task, node)
            except ValueError:
                raise ToolConfigurationError("Workflow artifact is unavailable") from None
            if declared.kind != "tool" or reference is None or reference.model_dump() != {
                "key": tool_key, "version": tool_version,
            }:
                raise ToolConfigurationError("Tool call does not match its declared node")
            found = session.execute(select(AiCapability, AiCapabilityVersion).join(
                AiCapabilityVersion, AiCapabilityVersion.capability_id == AiCapability.id,
            ).where(AiCapability.project_key == task.project_key, AiCapability.key == tool_key,
                    AiCapability.kind == "tool", AiCapabilityVersion.version == tool_version,
                    AiCapabilityVersion.status == "published", AiCapabilityVersion.content_hash == tool_hash)).one_or_none()
            if found is None:
                raise ToolConfigurationError("Pinned tool is unavailable")
            capability, artifact = found
            definition = ToolDefinition.model_validate(artifact.definition)
            binding = session.get(AiPlatformToolBinding, (task.project_key, binding_key))
            if (definition.binding_key != binding_key or binding is None or not binding.enabled or
                    binding.operation_type != "read_only" or definition.timeout_seconds > binding.max_timeout_seconds or
                    (binding.handler_key, binding.handler_version) != (handler_key, handler_version)):
                raise ToolConfigurationError("Tool binding is unavailable")
            if definition.implementation is not None and (handler_key, handler_version) != (
                definition.implementation.key, definition.implementation.version,
            ):
                raise ToolConfigurationError("Tool binding does not match its pinned implementation")
            call_id = uuid4().hex
            try:
                inserted = session.execute(text("""
                    INSERT INTO ai_platform_tool_call
                        (id, project_key, task_id, task_attempt_id, node_id, tool_capability_id,
                         tool_key, tool_version, tool_hash, binding_key, handler_key, handler_version,
                         operation_type, status, input_hash, start_time)
                    SELECT :id, t.project_key, t.id, a.id, n.id, :tool_id, :tool_key, :version,
                           :hash, b.key, b.handler_key, b.handler_version, 'read_only', 'running', :input_hash, clock_timestamp()
                    FROM ai_task t JOIN ai_task_attempt a ON a.task_id=t.id
                    JOIN ai_workflow_node n ON n.task_id=t.id AND n.task_attempt_id=a.id
                    JOIN ai_platform_tool_binding b ON b.project_key=t.project_key AND b.key=:binding
                    WHERE t.id=:task AND t.status='running' AND t.cancel_request_id IS NULL AND a.id=:attempt AND a.worker_id=:worker
                      AND a.token=:token AND a.status='running' AND a.lease_expires_at > clock_timestamp()
                      AND n.id=:node AND n.kind='tool' AND n.status='running' AND b.enabled
                      AND b.handler_key=:handler AND b.handler_version=:handler_version AND b.operation_type='read_only'
                    RETURNING id
                """), {"id": call_id, "tool_id": capability.id, "tool_key": tool_key, "version": tool_version,
                        "hash": tool_hash, "binding": binding_key, "input_hash": input_hash, "task": task.id,
                        "attempt": attempt.id, "worker": lease.worker_id, "token": lease.token,
                        "node": node_id, "handler": handler_key, "handler_version": handler_version}).scalar_one_or_none()
            except IntegrityError:
                raise ToolConflict("Tool node already has invocation evidence") from None
            if inserted is None:
                return None
            record_tool_call_transition(session, task.project_key, lease.worker_id, "tool_call_started",
                        task_id=task.id, call_id=call_id, binding_key=binding_key)
            return call_id

    def finish_call(self, lease, call_id, *, status, output_hash=None, error_code=None):
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("Invalid terminal tool status")
        if status == "succeeded":
            if not isinstance(output_hash, str) or not _HASH.fullmatch(output_hash) or error_code is not None:
                raise ValueError("Success requires only a valid output hash")
        elif output_hash is not None or not isinstance(error_code, str) or not _ERROR.fullmatch(error_code):
            raise ValueError("Tool failure requires a safe error code")
        with self._sf.begin() as session:
            scope = active_execution(session, lease, mode=ExecutionWriteMode.SETTLE)
            if scope is None:
                return False
            task, attempt = scope
            discarded = task.cancel_request_id is not None and status == "succeeded"
            if discarded:
                status, output_hash, error_code = "cancelled", None, "TOOL_CANCELLED_RESPONSE_DISCARDED"
            updated = session.execute(text("""
                UPDATE ai_platform_tool_call c SET status=:status, output_hash=:hash,
                    error_code=:error, finish_time=clock_timestamp()
                WHERE c.id=:call AND c.task_id=:task AND c.task_attempt_id=:attempt AND c.status='running'
                  AND EXISTS (SELECT 1 FROM ai_task t JOIN ai_task_attempt a ON a.task_id=t.id
                    JOIN ai_workflow_node n ON n.task_id=t.id AND n.task_attempt_id=a.id
                    WHERE t.id=c.task_id AND t.status='running' AND a.id=c.task_attempt_id
                      AND a.id=:attempt AND a.worker_id=:worker AND a.token=:token
                      AND a.status='running' AND a.lease_expires_at > clock_timestamp()
                      AND n.id=c.node_id AND n.status='running') RETURNING c.id
            """), {"status": status, "hash": output_hash, "error": error_code, "call": call_id,
                    "task": task.id, "attempt": attempt.id, "worker": lease.worker_id, "token": lease.token}).scalar_one_or_none()
            if updated is None:
                return False
            record_tool_call_transition(session, task.project_key, lease.worker_id, "tool_call_" + status, task_id=task.id, call_id=call_id)
        if discarded:
            raise TaskCancellationRequested("Tool response settled but not accepted")
        return True

    def calls(self, project_key, task_id, *, allowed_keys, owner):
        from infra.store.repository.task import TaskRepository

        with self._read() as session:
            scope = TaskRepository._visible(project_key, allowed_keys, owner).where(AiTask.id == task_id)
            if session.execute(scope.with_only_columns(AiTask.id)).scalar_one_or_none() is None:
                raise TaskNotFound("Task not found")
            rows = session.scalars(select(AiPlatformToolCall).where(
                AiPlatformToolCall.project_key == project_key, AiPlatformToolCall.task_id == task_id,
            ).order_by(AiPlatformToolCall.start_time, AiPlatformToolCall.id).limit(500))
            return [row.to_record() for row in rows]
