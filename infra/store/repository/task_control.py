"""Durable cancellation commands, distinct from task admission and execution IO."""

import math
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, text

from domain.task import TaskConflict, TaskError, TaskNotFound
from domain.task_control import CancellationView, ControlState, ExecutionObservation, TaskCancellationRecord
from infra.store.model import AiTask, AiTaskAttempt
from infra.store.repository.base import BaseRepository
from infra.store.repository.execution import ExecutionWriteMode, active_execution
from infra.store.repository.execution_convergence import converge_execution_evidence
from infra.store.repository.execution_effects import record_task_transition
from infra.store.repository.task import TaskRepository


class TaskControlRepository(BaseRepository):
    @staticmethod
    def _receipt(session, task, capability):
        cancellation = None
        if task.cancel_request_id is not None:
            observed = acknowledged = None
            row = session.execute(select(AiTaskAttempt.cancel_observed_at, AiTaskAttempt.cancel_acknowledged_at).where(
                AiTaskAttempt.task_id == task.id,
            ).order_by(AiTaskAttempt.number.desc()).limit(1)).one_or_none()
            if row is not None:
                observed, acknowledged = row
            if task.status == "cancelled":
                state = "acknowledged"
                acknowledged = acknowledged or task.finish_time  # queued cancellation has no Attempt
            elif task.status != "running":
                state = "unconfirmed"
            else:
                state = "observed" if observed is not None else "requested"
            cancellation = CancellationView(request_id=task.cancel_request_id,
                                            requested_at=task.cancel_requested_at, requested_by=task.cancel_requested_by,
                                            state=state, observed_at=observed, acknowledged_at=acknowledged)
        return TaskCancellationRecord(task=task.to_domain(capability.key, capability.kind), cancellation=cancellation)

    def cancellation(self, project_key, task_id, *, allowed_keys, owner):
        with self._sf() as session, session.begin():
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            result = session.execute(TaskRepository._visible(project_key, allowed_keys, owner).where(
                AiTask.id == task_id,
            )).one_or_none()
            if result is None:
                raise TaskNotFound("Task not found")
            return self._receipt(session, *result)

    def request_cancel(self, project_key, task_id, actor, *, allowed_keys, owner):
        if not isinstance(actor, str) or not 1 <= len(actor) <= 200:
            raise TaskError("Invalid cancellation actor")
        with self._sf.begin() as session:
            result = session.execute(TaskRepository._visible(project_key, allowed_keys, owner).where(
                AiTask.id == task_id,
            ).with_for_update(of=AiTask)).one_or_none()
            if result is None:
                raise TaskNotFound("Task not found")
            task, capability = result
            if task.status not in {"queued", "running", "cancelled"}:
                raise TaskConflict("Terminal tasks cannot be cancelled")
            if task.cancel_request_id is None:
                requested_at = max(task.create_time, session.scalar(text("SELECT clock_timestamp()")))
                task.cancel_request_id = uuid4().hex
                task.cancel_requested_at = requested_at
                task.cancel_requested_by = actor
                was_queued = task.status == "queued"
                if was_queued:
                    task.status, task.finish_time = "cancelled", task.cancel_requested_at
                record_task_transition(session, task, actor, "task_cancel_requested")
                if was_queued:
                    record_task_transition(session, task, actor, "task_cancelled")
            session.flush()
            return self._receipt(session, task, capability)

    def observe(self, lease, *, lease_seconds, cancellation_grace_seconds):
        """Control renewal remains possible during cleanup, not indefinitely after observation."""
        for value, low, high in ((lease_seconds, 1, 3600), (cancellation_grace_seconds, 0.001, 60)):
            if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Invalid execution control timing")
        with self._sf.begin() as session:
            scope = active_execution(session, lease, mode=ExecutionWriteMode.CONTROL)
            if scope is None:
                return ExecutionObservation(ControlState.LEASE_LOST)
            task, attempt = scope
            cancelling = task.cancel_request_id is not None
            first = cancelling and attempt.cancel_observed_at is None
            observed = attempt.cancel_observed_at
            if first:
                observed = max(task.cancel_requested_at, attempt.start_time,
                               session.scalar(text("SELECT clock_timestamp()")))
            deadline = observed + timedelta(seconds=cancellation_grace_seconds) if observed is not None else None
            updated = session.execute(text("""
                UPDATE ai_task_attempt SET heartbeat_time=clock_timestamp(), cancel_observed_at=:observed,
                    lease_expires_at=CASE WHEN :cancelling THEN
                        GREATEST(lease_expires_at, LEAST(clock_timestamp()+(:seconds * interval '1 second'),
                                                      CAST(:deadline AS timestamptz)))
                        ELSE clock_timestamp()+(:seconds * interval '1 second') END
                WHERE id=:attempt AND task_id=:task AND worker_id=:worker AND token=:token
                  AND status='running' AND lease_expires_at > clock_timestamp()
                  AND EXISTS (SELECT 1 FROM ai_task WHERE id=:task AND status='running')
                RETURNING id
            """), {"observed": observed, "deadline": deadline, "cancelling": cancelling, "seconds": lease_seconds,
                    "task": lease.task_id, "attempt": lease.attempt_id, "worker": lease.worker_id,
                    "token": lease.token}).scalar_one_or_none()
            if updated is None:
                return ExecutionObservation(ControlState.LEASE_LOST)
            if first:
                record_task_transition(session, task, lease.worker_id, "task_cancel_observed")
            return ExecutionObservation(ControlState.CANCELLATION_REQUESTED if cancelling else ControlState.ACTIVE,
                                        task.cancel_request_id)

    def acknowledge_cancel(self, lease):
        """Only call after all local execution resources have stopped."""
        return self._resolve(lease, confirmed=True)

    def abandon_cancel(self, lease):
        """Record failed cleanup without ever publishing a false acknowledgement."""
        return self._resolve(lease, confirmed=False)

    def _resolve(self, lease, *, confirmed):
        with self._sf.begin() as session:
            scope = active_execution(session, lease, mode=ExecutionWriteMode.CONTROL)
            if scope is None:
                return False
            task, attempt = scope
            if task.cancel_request_id is None:
                raise TaskConflict("No cancellation intent exists")
            now = max(session.scalar(text("SELECT clock_timestamp()")), task.cancel_requested_at, attempt.start_time,
                      attempt.cancel_observed_at or attempt.start_time)
            status = "cancelled" if confirmed else "failed"
            error = None if confirmed else "CANCEL_CLEANUP_RESULT_UNKNOWN"
            updated = session.execute(text("""
                UPDATE ai_task_attempt SET status=:status, finish_time=:finished, error_code=:error,
                    cancel_observed_at=COALESCE(cancel_observed_at,:finished), cancel_acknowledged_at=:acknowledged
                WHERE id=:attempt AND task_id=:task AND worker_id=:worker AND token=:token
                  AND status='running' AND lease_expires_at > clock_timestamp()
                  AND EXISTS (SELECT 1 FROM ai_task WHERE id=:task AND status='running' AND cancel_request_id IS NOT NULL)
                RETURNING id
            """), {"status": status, "finished": now, "error": error, "acknowledged": now if confirmed else None,
                    "task": lease.task_id, "attempt": lease.attempt_id, "worker": lease.worker_id,
                    "token": lease.token}).scalar_one_or_none()
            if updated is None:
                return False
            if attempt.cancel_observed_at is None:
                record_task_transition(session, task, lease.worker_id, "task_cancel_observed")
            converge_execution_evidence(session, task.id, attempt.id, now,
                                        "CANCELLED" if confirmed else "CANCEL_CLEANUP", cancelled=confirmed)
            task.status, task.finish_time, task.error_code = status, now, error
            record_task_transition(session, task, lease.worker_id,
                                  "task_cancelled" if confirmed else "task_cancel_unconfirmed")
            session.flush()
            return True
