"""Shared task/attempt ownership lookup for fenced execution evidence writers."""

from enum import StrEnum

from sqlalchemy import select, text
from sqlalchemy.orm import defer

from domain.task_execution import TaskCancellationRequested, TaskLeaseLost
from infra.store.model import AiTask, AiTaskAttempt


class ExecutionWriteMode(StrEnum):
    ADMIT = "admit"
    SETTLE = "settle"
    CONTROL = "control"


def active_execution(session, lease, *, lock=True, mode=ExecutionWriteMode.ADMIT):
    mode = ExecutionWriteMode(mode)
    task_query = select(AiTask).options(defer(AiTask.input, raiseload=True), defer(AiTask.result, raiseload=True)).where(
        AiTask.id == lease.task_id,
    )
    if lock:
        task_query = task_query.with_for_update()
    task = session.execute(task_query).scalar_one_or_none()
    if task is None or task.status != "running":
        return None
    query = select(AiTaskAttempt).where(
        AiTaskAttempt.id == lease.attempt_id, AiTaskAttempt.task_id == lease.task_id,
        AiTaskAttempt.worker_id == lease.worker_id, AiTaskAttempt.token == lease.token,
        AiTaskAttempt.status == "running", AiTaskAttempt.lease_expires_at > text("clock_timestamp()"),
    )
    attempt = session.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if attempt is None:
        return None
    if mode == ExecutionWriteMode.ADMIT and task.cancel_request_id is not None:
        raise TaskCancellationRequested("Task no longer admits execution writes")
    return task, attempt


def fence_execution_write(session, lease, *, mode=ExecutionWriteMode.ADMIT):
    """Final conditional write for a compound evidence transaction; does not renew."""
    mode = ExecutionWriteMode(mode)
    session.flush()
    updated = session.execute(text("""
        UPDATE ai_task_attempt SET heartbeat_time=heartbeat_time
        WHERE id=:attempt AND task_id=:task AND worker_id=:worker AND token=:token
          AND status='running' AND lease_expires_at > clock_timestamp()
          AND EXISTS (SELECT 1 FROM ai_task WHERE id=:task AND status='running'
                      AND (:admit=false OR cancel_request_id IS NULL))
        RETURNING id
    """), {"task": lease.task_id, "attempt": lease.attempt_id,
            "worker": lease.worker_id, "token": lease.token,
            "admit": mode == ExecutionWriteMode.ADMIT}).scalar_one_or_none()
    if updated is None:
        raise TaskLeaseLost("Compound evidence transaction lost its lease")
