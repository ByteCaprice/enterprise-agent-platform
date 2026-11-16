"""Durable, lease-fenced node checkpoints for deterministic workflows."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

from sqlalchemy import null, select, text

from domain import LogEvent
from domain.business_code import (
    CODE_WORKFLOW_CHECKPOINT_PROFILE,
    CODE_WORKFLOW_INTERPRETER_PROFILE,
    MAX_CODE_NODES,
    CodeNodeScope,
    CodeWorkflowDefinition,
    is_activity_unavailable,
)
from domain.capability_definitions import (
    WORKFLOW_CHECKPOINT_PROFILE,
    WORKFLOW_INTERPRETER_PROFILE,
    interpret_definition,
)
from domain.task import TaskNotFound, canonical_json
from domain.workflow import WorkflowCheckpoint, WorkflowNodeRecord
from infra.store.model import (
    AiAgentExecution,
    AiCapabilityVersion,
    AiPlatformModelCall,
    AiPlatformToolCall,
    AiRunLog,
    AiTask,
    AiTaskAttempt,
    AiWorkflowNode,
)
from infra.store.repository.base import BaseRepository
from infra.store.repository.execution import active_execution, fence_execution_write
from infra.store.repository.task_events import append_task_event
from infra.store.repository.workflow_scope import (
    _artifact,
    code_node_scope,
    declared_workflow_activity,
    validate_code_node,
)


class WorkflowRepository(BaseRepository):
    @staticmethod
    def _audit(session, task_id, attempt_id, node_id, event_type, *,
               interpreter_profile=WORKFLOW_INTERPRETER_PROFILE, checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE):
        session.add(
            AiRunLog.from_domain(
                LogEvent(
                    trace_id=f"task:{task_id}",
                    component="workflow_audit",
                    event_type=event_type,
                    message=event_type.replace("_", " "),
                    data={"task_id": task_id, "attempt_id": attempt_id, "node_id": node_id,
                           "interpreter_profile": interpreter_profile,
                           "checkpoint_profile": checkpoint_profile},
                )
            )
        )
        append_task_event(session, task_id, event_type, entity_kind="workflow_node", entity_id=node_id, attempt_id=attempt_id)

    def _reuse_pure_checkpoint(self, session, task, attempt, lease, artifact, node_key, input_hash):
        if task.origin_task_id is None:
            return None
        source = session.get(AiTask, task.origin_task_id)
        if (source is None or source.project_key != task.project_key or source.status not in {"failed", "timed_out", "cancelled"}
                or (source.capability_id, source.version, source.content_hash) !=
                   (task.capability_id, task.version, task.content_hash)):
            raise ValueError("Recovery source does not match the pinned workflow")
        definition = interpret_definition("workflow", artifact.to_domain()).materialize()
        if any(node.kind != "rule" or node.max_attempts != 1 for node in definition.nodes):
            raise ValueError("Checkpoint reuse requires a pure-rule workflow")
        original = session.scalar(select(AiWorkflowNode).join(
            AiTaskAttempt, AiTaskAttempt.id == AiWorkflowNode.task_attempt_id,
        ).where(
            AiWorkflowNode.task_id == source.id, AiWorkflowNode.node_key == node_key,
            AiWorkflowNode.kind == "rule", AiWorkflowNode.input_hash == input_hash,
            AiWorkflowNode.status.in_(("succeeded", "skipped")),
            AiTaskAttempt.status.in_(("failed", "timed_out", "cancelled", "succeeded")),
        ).order_by(AiTaskAttempt.number.desc()).limit(1))
        if original is None:
            return None
        if original.status == "succeeded":
            if not isinstance(original.output, dict) or hashlib.sha256(
                canonical_json(original.output).encode()).hexdigest() != original.output_hash:
                raise ValueError("Recovery checkpoint output is corrupt")
        elif original.output is not None or original.output_hash is not None:
            raise ValueError("Skipped recovery checkpoint is corrupt")
        now = session.scalar(text("SELECT clock_timestamp()"))
        node = AiWorkflowNode(id=uuid4().hex, task_id=task.id, task_attempt_id=attempt.id, node_key=node_key,
            kind="rule", status=original.status, input_hash=input_hash, output_hash=original.output_hash,
            output=deepcopy(original.output) if original.status == "succeeded" else null(),
            reused_from_node_id=original.id, start_time=now, finish_time=now)
        session.add(node)
        self._audit(session, task.id, attempt.id, node.id, "workflow_node_reused")
        fence_execution_write(session, lease)
        return WorkflowCheckpoint(node.to_record(), deepcopy(node.output), False)

    def begin(self, lease, node_key, input_hash, *, interpreter_profile, checkpoint_profile,
              scope: CodeNodeScope | None = None, parent_node_id=None):
        if (interpreter_profile, checkpoint_profile) not in {
            (WORKFLOW_INTERPRETER_PROFILE, WORKFLOW_CHECKPOINT_PROFILE),
            (CODE_WORKFLOW_INTERPRETER_PROFILE, CODE_WORKFLOW_CHECKPOINT_PROFILE),
        }:
            raise ValueError("Workflow checkpoint interpretation is incompatible")
        if not isinstance(input_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", input_hash):
            raise ValueError("Invalid input_hash")
        node_scope = code_node_scope(scope) if scope is not None else None
        with self._sf.begin() as session:
            execution = active_execution(session, lease)
            if execution is None:
                return None
            task, attempt = execution
            _, _, definition = _artifact(session, task, node_scope)
            code = isinstance(definition, CodeWorkflowDefinition)
            expected = ((CODE_WORKFLOW_INTERPRETER_PROFILE, CODE_WORKFLOW_CHECKPOINT_PROFILE) if code else
                        (WORKFLOW_INTERPRETER_PROFILE, WORKFLOW_CHECKPOINT_PROFILE))
            if (interpreter_profile, checkpoint_profile) != expected:
                raise ValueError("Workflow checkpoint does not match the published interpretation")
            node_id = uuid4().hex
            if code:
                if node_scope is None:
                    raise ValueError("Code Workflow checkpoints require a scope")
                declared = next((item for item in definition.activities if item.key == node_scope.activity_key), None)
                kind = "result" if node_scope.activity_key is None else declared.kind if declared else None
                proposed = AiWorkflowNode(id=node_id, task_id=task.id, task_attempt_id=attempt.id,
                    node_key=node_key, kind=kind, parent_node_id=parent_node_id,
                    scope=asdict(node_scope))
                declared, _, _ = validate_code_node(session, task, proposed)
                rows = list(session.scalars(select(AiWorkflowNode).where(
                    AiWorkflowNode.task_id == task.id, AiWorkflowNode.task_attempt_id == attempt.id,
                ).limit(MAX_CODE_NODES + 1)))
                if len(rows) >= MAX_CODE_NODES:
                    raise ValueError("Code Workflow Attempt node limit exceeded")
                if declared is not None:
                    calls = [row for row in rows if row.parent_node_id == parent_node_id and
                             row.scope is not None and row.scope.get("activity_key") == node_scope.activity_key]
                    if any(row.scope.get("invocation_hash") == node_scope.invocation_hash for row in calls):
                        raise ValueError("Code Workflow invocations cannot be replayed")
                    if len(calls) >= declared.max_calls:
                        raise ValueError("Code Workflow activity call limit exceeded")
                    if sum(row.parent_node_id == parent_node_id and row.status == "running" for row in rows) >= definition.max_parallelism:
                        raise ValueError("Code Workflow frame parallelism limit exceeded")
            else:
                if node_scope is not None or parent_node_id is not None:
                    raise ValueError("Legacy Workflow checkpoints do not accept code scope")
                node = next((item for item in definition.nodes if item.key == node_key), None)
                if node is None:
                    raise ValueError("Unknown workflow node")
                kind = node.kind
            existing = session.execute(
                select(AiWorkflowNode).where(
                    AiWorkflowNode.task_attempt_id == attempt.id,
                    AiWorkflowNode.node_key == node_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                if code:
                    raise ValueError("Code Workflow slots cannot be replayed")
                if existing.input_hash != input_hash or existing.status not in {"succeeded", "skipped"}:
                    raise ValueError("Workflow node checkpoint cannot be replayed")
                return WorkflowCheckpoint(existing.to_record(), existing.output, False)

            if task.origin_task_id is not None:
                if code:
                    raise ValueError("Code Workflows do not support checkpoint recovery")
                artifact = session.scalar(select(AiCapabilityVersion).where(
                    AiCapabilityVersion.capability_id == task.capability_id, AiCapabilityVersion.version == task.version,
                ))
                reused = self._reuse_pure_checkpoint(session, task, attempt, lease, artifact, node_key, input_hash)
                if reused is not None:
                    return reused

            inserted = session.execute(
                text("""
                    INSERT INTO ai_workflow_node
                        (id, task_id, task_attempt_id, node_key, kind, status, input_hash, start_time, parent_node_id, scope)
                    SELECT :node_id, t.id, a.id, :node_key, :kind, 'running', :input_hash, clock_timestamp(),
                           :parent_node_id, CAST(:scope AS jsonb)
                    FROM ai_task AS t
                    JOIN ai_task_attempt AS a ON a.task_id = t.id
                    WHERE t.id = :task_id AND t.status = 'running' AND t.cancel_request_id IS NULL
                      AND a.id = :attempt_id AND a.worker_id = :worker_id AND a.token = :token
                      AND a.status = 'running' AND a.lease_expires_at > clock_timestamp()
                    RETURNING id
                """),
                {
                    "node_id": node_id,
                    "task_id": lease.task_id,
                    "attempt_id": lease.attempt_id,
                    "worker_id": lease.worker_id,
                    "token": lease.token,
                    "node_key": node_key,
                    "kind": kind,
                    "input_hash": input_hash,
                    "parent_node_id": parent_node_id,
                    "scope": canonical_json(asdict(node_scope)) if node_scope is not None else None,
                },
            ).scalar_one_or_none()
            if inserted is None:
                return None
            self._audit(session, task.id, attempt.id, node_id, "workflow_node_started",
                        interpreter_profile=interpreter_profile, checkpoint_profile=checkpoint_profile)
            session.flush()
            row = session.get(AiWorkflowNode, node_id)
            return WorkflowCheckpoint(row.to_record(), None, True)

    def finish(self, lease, node_id, *, status, output=None, error_code=None):
        if status not in {"succeeded", "failed", "skipped", "unavailable", "cancelled"}:
            raise ValueError("Invalid workflow node status")
        if status == "succeeded":
            if not isinstance(output, dict) or error_code is not None:
                raise ValueError("Successful node requires output object")
            encoded = canonical_json(output)
            output_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        else:
            if output is not None:
                raise ValueError("Only successful nodes accept output")
            output_hash = None
            if status in {"failed", "unavailable", "cancelled"}:
                if not isinstance(error_code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", error_code):
                    raise ValueError("Invalid error_code")
            elif error_code is not None:
                raise ValueError("Non-failed nodes cannot have error_code")

        with self._sf.begin() as session:
            scope = active_execution(session, lease)
            if scope is None:
                return False
            task, attempt = scope
            row = session.scalar(select(AiWorkflowNode).where(
                AiWorkflowNode.id == node_id, AiWorkflowNode.task_id == task.id,
                AiWorkflowNode.task_attempt_id == attempt.id,
            ).with_for_update())
            if row is None or row.status != "running":
                return False
            code = row.scope is not None
            if code:
                declared, _, _ = validate_code_node(session, task, row)
                if status == "skipped" and (declared is None or declared.required):
                    raise ValueError("Only optional code Workflow activities can be skipped")
                if status == "unavailable" and (declared is None or declared.kind == "step" or
                                                declared.on_unavailable != "continue" or
                                                not is_activity_unavailable(error_code)):
                    raise ValueError("Activity does not permit classified unavailability")
                if status == "cancelled" and error_code != "CODE_ACTIVITY_GROUP_CANCELLED":
                    raise ValueError("Only safe code activity group cancellation can be checkpointed")
            else:
                declared_workflow_activity(session, task, row)
                if status in {"unavailable", "cancelled"}:
                    raise ValueError("Legacy Workflow does not accept code terminal statuses")
            if code:
                for entity, parent_column in (
                    (AiPlatformModelCall, AiPlatformModelCall.node_id),
                    (AiPlatformToolCall, AiPlatformToolCall.node_id),
                    (AiAgentExecution, AiAgentExecution.parent_node_id),
                    (AiWorkflowNode, AiWorkflowNode.parent_node_id),
                ):
                    if session.scalar(select(entity.id).where(
                        entity.task_id == task.id, entity.task_attempt_id == attempt.id,
                        parent_column == row.id, entity.status == "running",
                    ).limit(1)) is not None:
                        raise ValueError("Code Workflow node has unsettled child evidence")
                if row.kind == "workflow" and status == "succeeded":
                    children = list(session.scalars(select(AiWorkflowNode).where(
                        AiWorkflowNode.parent_node_id == row.id, AiWorkflowNode.task_id == task.id,
                        AiWorkflowNode.task_attempt_id == attempt.id,
                    )))
                    if (len(children) != 1 or children[0].kind != "result" or children[0].status != "succeeded" or
                            children[0].output_hash != output_hash):
                        raise ValueError("Workflow activity output must match its completed child frame")
            if status == "succeeded":
                node = session.scalar(select(AiWorkflowNode).where(AiWorkflowNode.id == node_id,
                    AiWorkflowNode.task_id == task.id, AiWorkflowNode.task_attempt_id == attempt.id))
                if node is not None and node.kind == "agent":
                    child = session.scalar(select(AiAgentExecution).where(
                        AiAgentExecution.parent_node_id == node.id, AiAgentExecution.task_attempt_id == attempt.id,
                        AiAgentExecution.status == "succeeded",
                    ))
                    if child is None or child.output_hash != output_hash:
                        raise ValueError("Agent node output must match its completed child execution")
            updated = session.execute(
                text("""
                    UPDATE ai_workflow_node AS n
                    SET status = :status, output = CAST(:output AS jsonb), output_hash = :output_hash,
                        error_code = :error_code, finish_time = clock_timestamp()
                    WHERE n.id = :node_id AND n.task_id = :task_id
                      AND n.task_attempt_id = :attempt_id AND n.status = 'running'
                      AND EXISTS (
                        SELECT 1 FROM ai_task AS t
                        JOIN ai_task_attempt AS a ON a.task_id = t.id
                        WHERE t.id = n.task_id AND t.status = 'running' AND t.cancel_request_id IS NULL
                          AND a.id = n.task_attempt_id AND a.id = :attempt_id
                          AND a.worker_id = :worker_id AND a.token = :token
                          AND a.status = 'running' AND a.lease_expires_at > clock_timestamp()
                      )
                    RETURNING n.id
                """),
                {
                    "status": status,
                    "output": canonical_json(output) if status == "succeeded" else None,
                    "output_hash": output_hash,
                    "error_code": error_code,
                    "node_id": node_id,
                    "task_id": task.id,
                    "attempt_id": attempt.id,
                    "worker_id": lease.worker_id,
                    "token": lease.token,
                },
            ).scalar_one_or_none()
            if updated is None:
                return False
            self._audit(session, task.id, attempt.id, node_id, "workflow_node_" + status,
                        interpreter_profile=CODE_WORKFLOW_INTERPRETER_PROFILE if code else WORKFLOW_INTERPRETER_PROFILE,
                        checkpoint_profile=CODE_WORKFLOW_CHECKPOINT_PROFILE if code else WORKFLOW_CHECKPOINT_PROFILE)
            return True

    def list(self, project_key, task_id, *, allowed_keys, owner):
        from infra.store.repository.task import TaskRepository

        with self._read() as session:
            scope = TaskRepository._visible(project_key, allowed_keys, owner).where(AiTask.id == task_id)
            if session.execute(scope.with_only_columns(AiTask.id)).scalar_one_or_none() is None:
                raise TaskNotFound("Task not found")
            columns = [
                getattr(AiWorkflowNode, name)
                for name in (
                    "id",
                    "task_id",
                    "task_attempt_id",
                    "node_key",
                    "kind",
                    "status",
                    "input_hash",
                    "output_hash",
                    "reused_from_node_id",
                    "start_time",
                    "finish_time",
                    "error_code",
                    "parent_node_id",
                    "scope",
                )
            ]
            rows = session.execute(
                select(*columns)
                .join(AiTaskAttempt, AiTaskAttempt.id == AiWorkflowNode.task_attempt_id)
                .where(AiWorkflowNode.task_id == task_id)
                .order_by(AiTaskAttempt.number, AiWorkflowNode.start_time, AiWorkflowNode.node_key)
                .limit(500)
            ).mappings()
            return [WorkflowNodeRecord.model_validate(dict(row)) for row in rows]
