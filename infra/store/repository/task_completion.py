"""Atomic completion adapter: detached validation, live evidence, final DB fence.

Expensive JSON Schema matching runs before the write transaction. The write
transaction rechecks the exact artifact hash, authoritative evidence, ownership,
cancellation, and lease before committing the already-detached result bytes.
"""

from __future__ import annotations

from sqlalchemy import select, text, tuple_

from domain.schema_validation import JSONValidationUnavailable
from domain.task import TaskValidationUnavailable
from domain.task_completion import (
    AgentCompletionEvidence,
    CompletionCommand,
    CompletionEvidence,
    CompletionTarget,
    NodeCompletionEvidence,
    require_completion_evidence,
)
from domain.task_execution import CompletionOutcome
from infra.schema_validation import validate_json
from infra.store.model import (
    AiAgentExecution,
    AiAgentStep,
    AiCapability,
    AiCapabilityVersion,
    AiPlatformModelCall,
    AiPlatformToolCall,
    AiWorkflowNode,
)
from infra.store.repository.base import BaseRepository
from infra.store.repository.execution import ExecutionWriteMode, active_execution
from infra.store.repository.execution_convergence import converge_execution_evidence
from infra.store.repository.execution_effects import record_task_transition
from infra.store.repository.workflow_scope import code_node_scope


class TaskCompletionRepository(BaseRepository):
    @staticmethod
    def _artifact(session, task):
        capability, row = session.execute(
            select(AiCapability, AiCapabilityVersion)
            .join(AiCapabilityVersion, AiCapabilityVersion.capability_id == AiCapability.id)
            .where(AiCapability.id == task.capability_id, AiCapabilityVersion.version == task.version)
        ).one()
        artifact = row.to_domain()
        artifact.require_published(
            capability_id=task.capability_id, version=task.version, content_hash=task.content_hash,
        )
        return capability, artifact

    @staticmethod
    def _closed(scope):
        if scope is None:
            return CompletionOutcome.LEASE_LOST
        if scope[0].cancel_request_id is not None:
            return CompletionOutcome.CANCELLATION_PENDING
        return None

    def _validate_result(self, lease, command):
        # A read snapshot is sufficient for preparation, NEVER for the final write.
        # Close its connection before waiting on validator capacity/process startup.
        with self._read() as session:
            scope = active_execution(session, lease, lock=False, mode=ExecutionWriteMode.CONTROL)
            closed = self._closed(scope)
            if closed is not None:
                return closed
            _, artifact = self._artifact(session, scope[0])
        try:
            validate_json(command.result, artifact.output_schema)
        except JSONValidationUnavailable as exc:
            raise TaskValidationUnavailable(exc.code) from None
        return artifact.capability_id, artifact.version, artifact.content_hash

    @staticmethod
    def _evidence(session, task, attempt):
        def exists(model, condition):
            return session.scalar(select(model.id).where(model.task_id == task.id, condition).limit(1)) is not None

        nodes = session.execute(select(
            AiWorkflowNode.id, AiWorkflowNode.node_key, AiWorkflowNode.kind,
            AiWorkflowNode.status, AiWorkflowNode.output_hash,
            AiWorkflowNode.parent_node_id, AiWorkflowNode.scope, AiWorkflowNode.error_code,
        ).where(AiWorkflowNode.task_attempt_id == attempt.id)).all()
        agents = session.scalars(select(AiAgentExecution).where(
            AiAgentExecution.task_id == task.id, AiAgentExecution.task_attempt_id == attempt.id,
        )).all()
        node_evidence = tuple(NodeCompletionEvidence(
            id=row.id, key=row.node_key, kind=row.kind, status=row.status, output_hash=row.output_hash,
            parent_node_id=row.parent_node_id, scope=code_node_scope(row.scope) if row.scope is not None else None,
            error_code=row.error_code,
        ) for row in nodes)
        frames = {
            (node.scope.workflow_key, node.scope.version, node.scope.content_hash)
            for node in node_evidence if node.kind == "result" and node.scope is not None
        }
        artifacts = []
        if frames:
            found = session.execute(select(AiCapability, AiCapabilityVersion).join(
                AiCapabilityVersion, AiCapabilityVersion.capability_id == AiCapability.id,
            ).where(
                AiCapability.project_key == task.project_key, AiCapability.kind == "workflow",
                AiCapabilityVersion.status == "published",
                tuple_(AiCapability.key, AiCapabilityVersion.version, AiCapabilityVersion.content_hash).in_(frames),
            )).all()
            if len(found) != len(frames):
                raise ValueError("Scoped Workflow frame artifacts are unavailable")
            artifacts = [(capability.key, row.to_domain()) for capability, row in found]
        return CompletionEvidence(
            running_model_call=exists(AiPlatformModelCall, AiPlatformModelCall.status == "running"),
            running_tool_call=exists(AiPlatformToolCall, AiPlatformToolCall.status == "running"),
            unfinished_agent=exists(AiAgentExecution, AiAgentExecution.status != "succeeded"),
            unfinished_agent_step=exists(AiAgentStep, AiAgentStep.status != "succeeded"),
            running_agent=exists(AiAgentExecution, AiAgentExecution.status == "running"),
            running_agent_step=exists(AiAgentStep, AiAgentStep.status.in_(("pending", "running"))),
            nodes=node_evidence,
            agents=tuple(AgentCompletionEvidence(
                capability_id=row.agent_capability_id, key=row.agent_key, version=row.agent_version,
                content_hash=row.agent_hash, output_hash=row.output_hash, parent_node_id=row.parent_node_id,
                status=row.status, error_code=row.error_code,
            ) for row in agents),
            workflow_artifacts=tuple(artifacts),
        )

    def complete(self, lease, *, status, result=None, error_code=None) -> CompletionOutcome:
        command = CompletionCommand.prepare(status=status, result=result, error_code=error_code)
        validated_artifact = None
        if command.status == "succeeded":
            prepared = self._validate_result(lease, command)
            if isinstance(prepared, CompletionOutcome):
                return prepared
            validated_artifact = prepared
        with self._sf.begin() as session:
            scope = active_execution(session, lease, mode=ExecutionWriteMode.CONTROL)
            closed = self._closed(scope)
            if closed is not None:
                return closed
            task, attempt = scope
            if command.status == "succeeded":
                capability, artifact = self._artifact(session, task)
                if (artifact.capability_id, artifact.version, artifact.content_hash) != validated_artifact:
                    raise ValueError("Validated completion artifact changed")
                require_completion_evidence(
                    CompletionTarget(task.capability_id, capability.kind, task.version, task.content_hash,
                                     capability_key=capability.key),
                    artifact, command, self._evidence(session, task, attempt),
                )
            # The same conditional write still provides the final DB-clock fence.
            fenced = session.execute(text("""
                UPDATE ai_task_attempt SET status=:status, finish_time=clock_timestamp(), error_code=:error
                WHERE id=:attempt AND task_id=:task AND worker_id=:worker AND token=:token
                  AND status='running' AND lease_expires_at > clock_timestamp()
                  AND EXISTS (SELECT 1 FROM ai_task WHERE id=:task AND status='running' AND cancel_request_id IS NULL)
                RETURNING finish_time
            """), {"status": command.status, "error": command.error_code, "attempt": lease.attempt_id,
                  "task": lease.task_id, "worker": lease.worker_id, "token": lease.token}).scalar_one_or_none()
            if fenced is None:
                return CompletionOutcome.LEASE_LOST
            if command.status != "succeeded":
                converge_execution_evidence(session, task.id, attempt.id, fenced, command.error_code)
            task.status, task.finish_time, task.result = command.status, fenced, command.result
            task.error_code = command.error_code
            attempt.status, attempt.finish_time, attempt.error_code = command.status, fenced, command.error_code
            record_task_transition(session, task, lease.worker_id, "task_" + command.status)
            session.flush()
            return CompletionOutcome.COMPLETED
