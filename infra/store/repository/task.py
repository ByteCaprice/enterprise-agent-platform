"""Task inbox persistence, scoped reads, idempotency locks and atomic audit."""

from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import defer

from domain import LogEvent, utc_now
from domain.schema_validation import JSONValidationUnavailable
from domain.task import TaskConflict, TaskNotFound, TaskValidationUnavailable
from domain.task_execution import CompletionOutcome, TaskAttemptRecord
from infra.store.model.ai_capability import AiCapability, AiCapabilityVersion
from infra.store.model.ai_governance import AiProject
from infra.store.model.ai_run_log import AiRunLog
from infra.store.model.ai_task import AiTask
from infra.store.repository.base import BaseRepository
from infra.store.repository.execution_convergence import converge_execution_evidence
from infra.store.repository.task_events import append_task_event


class TaskRepository(BaseRepository):
    def check_ready(self):
        with self._read() as session:
            session.execute(select(AiTask).limit(0))
            from infra.store.model.ai_task_attempt import AiTaskAttempt
            from infra.store.model.ai_workflow_node import AiWorkflowNode

            session.execute(select(AiTaskAttempt).limit(0))
            session.execute(select(AiWorkflowNode).limit(0))

    @staticmethod
    def _audit(session, row, actor, event_type):
        session.add(
            AiRunLog.from_domain(
                LogEvent(
                    trace_id=f"task:{row.id}",
                    component="task_audit",
                    event_type=event_type,
                    message=event_type.replace("_", " "),
                    data={"actor": actor, "project_key": row.project_key, "task_id": row.id},
                )
            )
        )
        append_task_event(session, row.id, event_type)

    def lock_submission(self, project_key, owner, idempotency_digest, *, conn):
        scope = hashlib.sha256(json.dumps([project_key, owner, idempotency_digest]).encode()).hexdigest()
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(:scope))"), {"scope": scope}
        )

    def find_submission(self, project_key, owner, idempotency_digest, fingerprint, *, conn):
        result = conn.execute(
            select(AiTask, AiCapability)
            .options(defer(AiTask.input, raiseload=True), defer(AiTask.result, raiseload=True))
            .join(AiCapability)
            .where(
                AiTask.project_key == project_key,
                AiTask.submitted_by == owner,
                AiTask.idempotency_digest == idempotency_digest,
            )
        ).one_or_none()
        if result is None:
            return None
        row, capability = result
        if row.request_fingerprint != fingerprint:
            raise TaskConflict("Idempotency key was used for a different request")
        return row.to_domain(capability.key, capability.kind)

    def target(self, project_key, capability_key, version, *, conn):
        result = conn.execute(
            select(AiCapability, AiCapabilityVersion)
            .join(AiCapabilityVersion)
            .where(
                AiCapability.project_key == project_key,
                AiCapability.key == capability_key,
                AiCapabilityVersion.version == version,
            )
        ).one_or_none()
        if result is None:
            raise TaskNotFound("Task capability version not found")
        capability, artifact = result
        return capability.to_dict(), artifact.to_domain()

    def create(self, project_key, owner, spec, capability, artifact, idempotency_digest, fingerprint, *, conn, origin_task_id=None):
        row = AiTask(
            id=uuid4().hex,
            project_key=project_key,
            capability_id=capability["id"],
            version=artifact.version,
            content_hash=artifact.content_hash,
            business_id=spec.business_id,
            customer_id=spec.customer_id,
            submitted_by=owner,
            idempotency_digest=idempotency_digest,
            request_fingerprint=fingerprint,
            input=spec.input,
            status="queued",
            create_time=utc_now(),
            finish_time=None,
            origin_task_id=origin_task_id,
        )
        conn.add(row)
        self._audit(conn, row, owner, "task_submitted")
        conn.flush()
        return row.to_domain(capability["key"], capability["kind"])

    @staticmethod
    def _visible(project_key, allowed_keys, owner):
        # Metadata routes must not load up to 500 large customer inputs into memory.
        query = (
            select(AiTask, AiCapability)
            .options(defer(AiTask.input, raiseload=True), defer(AiTask.result, raiseload=True))
            .join(AiCapability)
            .where(
                AiTask.project_key == project_key,
            )
        )
        if "*" not in allowed_keys:
            query = query.where(AiCapability.key.in_(allowed_keys))
        if owner is not None:
            query = query.where(AiTask.submitted_by == owner)
        return query

    def get(self, project_key, task_id, *, allowed_keys, owner):
        with self._read() as session:
            result = session.execute(
                self._visible(project_key, allowed_keys, owner).where(
                    AiTask.id == task_id,
                )
            ).one_or_none()
            if result is None:
                raise TaskNotFound("Task not found")
            return result[0].to_domain(result[1].key, result[1].kind)

    def list(self, project_key, *, allowed_keys, owner, limit=100, business_id=None, customer_id=None):
        with self._read() as session:
            query = self._visible(project_key, allowed_keys, owner)
            if business_id is not None:
                query = query.where(AiTask.business_id == business_id)
            if customer_id is not None:
                query = query.where(AiTask.customer_id == customer_id)
            results = session.execute(
                query
                .order_by(
                    AiTask.create_time.desc(),
                    AiTask.id.desc(),
                )
                .limit(limit)
            )
            return [row.to_domain(capability.key, capability.kind) for row, capability in results]

    def _execution_scope(self, session, project_key, task_id, allowed_keys, owner):
        query = self._visible(project_key, allowed_keys, owner).where(AiTask.id == task_id)
        return session.execute(query).one_or_none()

    def claim(self, worker_id, *, lease_seconds=30, kinds=("agent", "workflow"), workflow_agents=False):
        from math import isfinite

        from domain.task_execution import TaskClaim, TaskLease
        from infra.store.model.ai_task_attempt import AiTaskAttempt

        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 200:
            raise ValueError("Invalid worker_id")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not isfinite(lease_seconds)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        if not kinds or not set(kinds) <= {"agent", "workflow"}:
            raise ValueError("Invalid task kinds")
        if type(workflow_agents) is not bool:
            raise ValueError("Declare Workflow Agent support explicitly")
        with self._sf.begin() as session:
            from infra.store.model.ai_operations import AiGlobalConcurrency
            from infra.store.repository.limits import admit_claim, concurrency_lock

            concurrency_lock(session)
            global_limit = session.get(AiGlobalConcurrency, "global")
            if global_limit is not None and global_limit.max_running is not None:
                running = session.scalar(text("SELECT count(*) FROM ai_task WHERE status='running'"))
                if running >= global_limit.max_running:
                    return None
            eligible = (
                select(AiTask.id)
                .join(AiCapability)
                .join(AiProject, AiProject.key == AiTask.project_key)
                .join(AiCapabilityVersion, (AiCapabilityVersion.capability_id == AiTask.capability_id) &
                      (AiCapabilityVersion.version == AiTask.version))
                .where(AiTask.status == "queued", AiCapability.kind.in_(kinds))
                .where(AiProject.enabled.is_(True))
                .order_by(AiTask.create_time, AiTask.id)
                .with_for_update(skip_locked=True, of=AiProject)
                .limit(1)
            )
            if not workflow_agents:
                eligible = eligible.where(text("""(ai_capability.kind <> 'workflow' OR NOT jsonb_path_exists(
                    ai_capability_version.definition, '$.nodes[*] ? (@.kind == "agent")'))"""))
            eligible = eligible.where(text("""NOT EXISTS (SELECT 1 FROM ai_project_limits l
                WHERE l.project_key=ai_task.project_key AND l.max_running IS NOT NULL
                AND (SELECT count(*) FROM ai_task running WHERE running.project_key=ai_task.project_key
                     AND running.status='running') >= l.max_running)"""))
            eligible = eligible.where(text("""NOT EXISTS (SELECT 1 FROM ai_capability_concurrency l
                WHERE l.capability_id=ai_task.capability_id AND l.max_running IS NOT NULL
                AND (SELECT count(*) FROM ai_task running WHERE running.capability_id=ai_task.capability_id
                     AND running.status='running') +
                    (SELECT count(*) FROM ai_agent_execution a WHERE a.agent_capability_id=ai_task.capability_id
                     AND a.parent_node_id IS NOT NULL AND a.status='running') >= l.max_running)"""))
            selected_id = session.scalar(eligible)
            if selected_id is None:
                return None
            selected = session.scalar(select(AiTask).where(AiTask.id == selected_id, AiTask.status == "queued")
                                      .with_for_update(skip_locked=True))
            if selected is None:
                return None
            if not admit_claim(session, selected.project_key):
                return None
            result = session.execute(
                select(AiCapability, AiCapabilityVersion)
                .join(AiCapabilityVersion, AiCapabilityVersion.capability_id == AiCapability.id)
                .where(AiCapability.id == selected.capability_id, AiCapabilityVersion.version == selected.version)
            ).one()
            capability, artifact_row = result
            artifact = artifact_row.to_domain()
            attempt_id, token = uuid4().hex, uuid4().hex + uuid4().hex
            # Database clock is authoritative for lease boundaries.
            timing = session.execute(
                text("SELECT clock_timestamp() AS now, clock_timestamp() + (:seconds * interval '1 second') AS expiry"),
                {"seconds": lease_seconds},
            ).one()
            selected.status = "running"
            attempt_num = (
                session.scalar(
                    select(AiTaskAttempt.number)
                    .where(AiTaskAttempt.task_id == selected.id)
                    .order_by(AiTaskAttempt.number.desc())
                    .limit(1)
                )
                or 0
            )
            attempt = AiTaskAttempt(
                id=attempt_id,
                task_id=selected.id,
                number=attempt_num + 1,
                worker_id=worker_id,
                token=token,
                status="running",
                start_time=timing.now,
                heartbeat_time=timing.now,
                lease_expires_at=timing.expiry,
            )
            session.add(attempt)
            self._audit(session, selected, worker_id, "task_claimed")
            session.flush()
            return TaskClaim(
                task=selected.to_domain(capability.key, capability.kind),
                lease=TaskLease(selected.id, attempt_id, worker_id, token),
                input=selected.input,
                artifact=artifact,
            )

    def heartbeat(self, lease, *, lease_seconds=30):
        from math import isfinite

        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not isfinite(lease_seconds)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        with self._sf.begin() as session:
            result = session.execute(
                text(
                    "UPDATE ai_task_attempt SET heartbeat_time=clock_timestamp(), lease_expires_at=clock_timestamp()+(:seconds * interval '1 second') WHERE id=:attempt AND task_id=:task AND worker_id=:worker AND token=:token AND status='running' AND lease_expires_at > clock_timestamp() AND EXISTS (SELECT 1 FROM ai_task WHERE id=:task AND status='running' AND cancel_request_id IS NULL)"
                ),
                {
                    "seconds": lease_seconds,
                    "attempt": lease.attempt_id,
                    "task": lease.task_id,
                    "worker": lease.worker_id,
                    "token": lease.token,
                },
            )
            return result.rowcount == 1

    def complete(self, lease, *, status, result=None, error_code=None) -> CompletionOutcome:
        if status not in {"succeeded", "failed", "timed_out"}:
            raise ValueError("Invalid terminal status")
        from domain.task import canonical_json

        if status == "succeeded":
            if not isinstance(result, dict):
                raise ValueError("Successful result must be an object")
            canonical_json(result)
            if error_code is not None:
                raise ValueError("Successful tasks cannot have an error code")
        elif result is not None:
            raise ValueError("Failure result is not accepted")
        else:
            error_code = error_code or ("TASK_TIMED_OUT" if status == "timed_out" else "TASK_FAILED")
        if error_code is not None and (
            not isinstance(error_code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", error_code)
        ):
            raise ValueError("Invalid error_code")
        from infra.store.model.ai_task_attempt import AiTaskAttempt

        with self._sf.begin() as session:
            task = session.execute(
                select(AiTask).where(AiTask.id == lease.task_id).with_for_update()
            ).scalar_one_or_none()
            if task is None or task.status != "running":
                return CompletionOutcome.LEASE_LOST
            attempt = session.execute(
                select(AiTaskAttempt)
                .where(
                    AiTaskAttempt.id == lease.attempt_id,
                    AiTaskAttempt.task_id == lease.task_id,
                    AiTaskAttempt.worker_id == lease.worker_id,
                    AiTaskAttempt.token == lease.token,
                    AiTaskAttempt.status == "running",
                    AiTaskAttempt.lease_expires_at > text("clock_timestamp()"),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if attempt is None:
                return CompletionOutcome.LEASE_LOST
            if task.cancel_request_id is not None:
                return CompletionOutcome.CANCELLATION_PENDING
            cap, artifact_row = session.execute(
                select(AiCapability, AiCapabilityVersion)
                .join(AiCapabilityVersion, AiCapabilityVersion.capability_id == AiCapability.id)
                .where(AiCapability.id == task.capability_id, AiCapabilityVersion.version == task.version)
            ).one()
            from infra.store.model.ai_platform_model_call import AiPlatformModelCall

            if status == "succeeded" and session.scalar(
                select(AiPlatformModelCall.id).where(
                    AiPlatformModelCall.task_id == task.id, AiPlatformModelCall.status == "running"
                ).limit(1)
            ) is not None:
                raise ValueError("Task cannot succeed with a running model call")
            from infra.store.model.ai_platform_tool import AiPlatformToolCall

            if status == "succeeded" and session.scalar(select(AiPlatformToolCall.id).where(
                AiPlatformToolCall.task_id == task.id, AiPlatformToolCall.status == "running",
            ).limit(1)) is not None:
                raise ValueError("Task cannot succeed with a running tool call")
            if status == "succeeded":
                from infra.store.model.ai_platform_agent import AiAgentExecution, AiAgentStep

                if session.scalar(select(AiAgentExecution.id).where(
                    AiAgentExecution.task_id == task.id, AiAgentExecution.status != "succeeded",
                ).limit(1)) is not None or session.scalar(select(AiAgentStep.id).where(
                    AiAgentStep.task_id == task.id, AiAgentStep.status != "succeeded",
                ).limit(1)) is not None:
                    raise ValueError("Task cannot succeed with unfinished Agent evidence")
                if cap.kind == "agent" and artifact_row.definition.get("definition_version") == "agent.v1":
                    import hashlib

                    root = session.scalar(select(AiAgentExecution).where(
                        AiAgentExecution.task_id == task.id, AiAgentExecution.task_attempt_id == attempt.id,
                        AiAgentExecution.parent_node_id.is_(None), AiAgentExecution.status == "succeeded",
                    ))
                    if (root is None or artifact_row.content_hash != task.content_hash or
                            root.agent_capability_id != task.capability_id or root.agent_version != task.version or
                            root.agent_hash != task.content_hash or
                            root.output_hash != hashlib.sha256(canonical_json(result).encode()).hexdigest()):
                        raise ValueError("Agent Task result must match its completed root execution")
                if cap.kind == "workflow":
                    from infra.store.model.ai_workflow_node import AiWorkflowNode

                    if artifact_row.content_hash != task.content_hash:
                        raise ValueError("Workflow artifact hash does not match the pinned task")
                    declared_nodes = artifact_row.definition.get("nodes")
                    if (
                        not isinstance(declared_nodes, list)
                        or not declared_nodes
                        or any(
                            not isinstance(node, dict)
                            or not isinstance(node.get("key"), str)
                            or node.get("kind") not in {"rule", "agent", "tool", "llm"}
                            for node in declared_nodes
                        )
                    ):
                        raise ValueError("Workflow contains unsupported or invalid nodes")
                    declared = {node["key"]: node["kind"] for node in declared_nodes}
                    if len(declared) != len(declared_nodes):
                        raise ValueError("Workflow contains duplicate node keys")
                    recorded = session.execute(select(AiWorkflowNode.id, AiWorkflowNode.node_key, AiWorkflowNode.kind,
                                                      AiWorkflowNode.status, AiWorkflowNode.output_hash)
                                               .where(AiWorkflowNode.task_attempt_id == attempt.id)).all()
                    completed = {row.node_key: row.kind for row in recorded if row.status in {"succeeded", "skipped"}}
                    if completed != declared or len(recorded) != len(declared):
                        raise ValueError("Workflow task cannot succeed before exactly its declared nodes complete")
                    for node in recorded:
                        if node.kind != "agent" or node.status != "succeeded":
                            continue
                        child = session.scalar(select(AiAgentExecution).where(
                            AiAgentExecution.parent_node_id == node.id, AiAgentExecution.task_attempt_id == attempt.id,
                            AiAgentExecution.status == "succeeded",
                        ))
                        reference = next(item["capability"] for item in declared_nodes if item["key"] == node.node_key)
                        if (child is None or child.output_hash != node.output_hash or
                                (child.agent_key, child.agent_version) != (reference["key"], reference["version"])):
                            raise ValueError("Workflow Agent node requires its matching completed execution")
                schema = artifact_row.output_schema
                from infra.schema_validation import validate_json

                try:
                    validate_json(result, schema)
                except JSONValidationUnavailable as exc:
                    raise TaskValidationUnavailable(exc.code) from None
            # Recheck the fence in the same conditional write immediately before finalization.
            fenced = session.execute(
                text(
                    "UPDATE ai_task_attempt SET status=:status, finish_time=clock_timestamp(), error_code=:error WHERE id=:attempt AND task_id=:task AND worker_id=:worker AND token=:token AND status='running' AND lease_expires_at > clock_timestamp() AND EXISTS (SELECT 1 FROM ai_task WHERE id=:task AND status='running' AND cancel_request_id IS NULL)"
                ),
                {
                    "status": status,
                    "error": error_code,
                    "attempt": lease.attempt_id,
                    "task": lease.task_id,
                    "worker": lease.worker_id,
                    "token": lease.token,
                },
            )
            if fenced.rowcount != 1:
                return CompletionOutcome.LEASE_LOST
            finished = session.scalar(
                text("SELECT finish_time FROM ai_task_attempt WHERE id=:attempt"), {"attempt": lease.attempt_id}
            )
            if status != "succeeded":
                converge_execution_evidence(session, task.id, attempt.id, finished, error_code or "TASK_FAILED")
            task.status, task.finish_time, task.result = status, finished, result if status == "succeeded" else None
            task.error_code = error_code if status != "succeeded" else None
            attempt.status, attempt.finish_time, attempt.error_code = status, finished, task.error_code
            self._audit(session, task, lease.worker_id, "task_" + status)
            session.flush()
            return CompletionOutcome.COMPLETED

    def recover_expired(self, *, limit=100):
        from infra.store.model.ai_task_attempt import AiTaskAttempt

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("Invalid limit")
        with self._sf.begin() as session:
            ids = session.scalars(
                text(
                    "SELECT id FROM ai_task WHERE status='running' AND id IN (SELECT task_id FROM ai_task_attempt WHERE status='running' AND lease_expires_at <= clock_timestamp()) ORDER BY create_time,id LIMIT :limit FOR UPDATE SKIP LOCKED"
                ),
                {"limit": limit},
            ).all()
            count = 0
            for task_id in ids:
                task = session.get(AiTask, task_id)
                attempt = session.execute(
                    select(AiTaskAttempt)
                    .where(AiTaskAttempt.task_id == task_id, AiTaskAttempt.status == "running")
                    .with_for_update()
                ).scalar_one_or_none()
                if attempt is None or attempt.lease_expires_at > session.scalar(text("SELECT clock_timestamp()")):
                    continue
                now = max(session.scalar(text("SELECT clock_timestamp()")), task.create_time,
                          task.cancel_requested_at or task.create_time, attempt.cancel_observed_at or attempt.start_time)
                error = "CANCEL_LEASE_EXPIRED_RESULT_UNKNOWN" if task.cancel_request_id is not None else "LEASE_EXPIRED_RESULT_UNKNOWN"
                task.status, task.finish_time, task.error_code = "failed", now, error
                attempt.status, attempt.finish_time, attempt.error_code = "failed", now, task.error_code
                converge_execution_evidence(session, task.id, attempt.id, now, error)
                self._audit(session, task, "system", "task_lease_expired")
                count += 1
            return count

    def reject_disabled_project_tasks(self, *, limit=100):
        """Fail queued work for a disabled project without starting an Attempt.

        Lock the project before its tasks, as admission does. Re-enabling the
        project and rejecting its queued work therefore have a definite order.
        """
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("Invalid limit")
        with self._sf.begin() as session:
            project = session.scalar(
                select(AiProject)
                .where(AiProject.enabled.is_(False))
                .where(select(AiTask.id).where(AiTask.project_key == AiProject.key,
                                              AiTask.status == "queued").exists())
                .order_by(AiProject.key)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if project is None:
                return 0
            tasks = session.scalars(select(AiTask).where(
                AiTask.project_key == project.key, AiTask.status == "queued",
            ).order_by(AiTask.create_time, AiTask.id).limit(limit).with_for_update(skip_locked=True)).all()
            for task in tasks:
                finished = max(session.scalar(text("SELECT clock_timestamp()")), task.create_time)
                task.status = "failed"
                task.finish_time = finished
                task.error_code = "EXECUTION_NOT_AUTHORIZED"
                self._audit(session, task, "system", "task_failed")
            return len(tasks)

    def attempts(self, project_key, task_id, *, allowed_keys, owner):
        from infra.store.model.ai_task_attempt import AiTaskAttempt

        with self._read() as session:
            if self._execution_scope(session, project_key, task_id, allowed_keys, owner) is None:
                raise TaskNotFound("Task not found")
            rows = session.execute(
                select(*(getattr(AiTaskAttempt, name) for name in TaskAttemptRecord.model_fields))
                .where(AiTaskAttempt.task_id == task_id)
                .order_by(AiTaskAttempt.number)
            ).mappings()
            return [TaskAttemptRecord.model_validate(dict(row)) for row in rows]

    def result(self, project_key, task_id, *, allowed_keys, owner):
        from domain.task_execution import TaskResultRecord

        with self._read() as session:
            query = (
                select(AiTask.id, AiTask.status, AiTask.result, AiTask.error_code)
                .join(AiCapability)
                .where(AiTask.project_key == project_key, AiTask.id == task_id)
            )
            if "*" not in allowed_keys:
                query = query.where(AiCapability.key.in_(allowed_keys))
            if owner is not None:
                query = query.where(AiTask.submitted_by == owner)
            row = session.execute(query).one_or_none()
            if row is None:
                raise TaskNotFound("Task not found")
            return TaskResultRecord(task_id=row.id, status=row.status, result=row.result, error_code=row.error_code)
