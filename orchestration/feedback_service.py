"""Append-only business adoption feedback; never changes a Task or quality score."""

import hashlib
from uuid import uuid4

from sqlalchemy import func, select

from domain import utc_now
from domain.task import TaskConflict, TaskNotFound, canonical_json
from infra.store.model import AiTask
from infra.store.model.ai_operations import AiTaskFeedback
from infra.store.repository.operations import audit
from orchestration.task_service import TaskService


def public_feedback(row):
    return {name: getattr(row, name) for name in (
        "id", "project_key", "task_id", "submitter", "revision", "adoption", "final_result", "reason", "create_time",
    )}


class FeedbackService:
    def __init__(self, store, auth):
        self.store, self.auth = store, auth
        self.tasks = TaskService(store, auth)

    def list(self, principal, project, task_id):
        task = self.tasks.get(principal, project, task_id)
        self.auth.allowed_resources(principal, project, "feedback:read", task.capability_key)
        with self.store.unit_of_work() as session:
            rows = session.scalars(select(AiTaskFeedback).where(
                AiTaskFeedback.project_key == project, AiTaskFeedback.task_id == task_id,
            ).order_by(AiTaskFeedback.create_time, AiTaskFeedback.id).limit(200))
            return [public_feedback(row) for row in rows]

    def add(self, principal, project, task_id, spec):
        task = self.tasks.get(principal, project, task_id)
        self.auth.allowed_resources(principal, project, "feedback:write", task.capability_key)
        owner = self.tasks.owner(principal)
        digest = hashlib.sha256(spec.request_key.encode()).hexdigest()
        fingerprint = hashlib.sha256(canonical_json(spec.model_dump(mode="json")).encode()).hexdigest()
        with self.store.unit_of_work() as session:
            row = session.scalar(select(AiTask).where(AiTask.id == task_id, AiTask.project_key == project).with_for_update())
            if row is None:
                raise TaskNotFound("Task not found")
            existing = session.scalar(select(AiTaskFeedback).where(
                AiTaskFeedback.task_id == task_id, AiTaskFeedback.submitter == owner, AiTaskFeedback.request_digest == digest,
            ))
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise TaskConflict("Feedback request key already used")
                return public_feedback(existing), False
            if row.status in {"queued", "running"}:
                raise TaskConflict("Feedback requires a terminal Task")
            if spec.adoption != "not_adopted" and row.status != "succeeded":
                raise TaskConflict("Only a successful Task can be adopted")
            revision = session.scalar(select(func.coalesce(func.max(AiTaskFeedback.revision), 0)).where(
                AiTaskFeedback.task_id == task_id, AiTaskFeedback.submitter == owner,
            ))
            if spec.expected_revision != revision:
                raise TaskConflict("Feedback revision changed")
            feedback = AiTaskFeedback(id=uuid4().hex, project_key=project, task_id=task_id, submitter=owner,
                revision=revision + 1, request_digest=digest, fingerprint=fingerprint, adoption=spec.adoption,
                final_result=spec.final_result, reason=spec.reason, create_time=utc_now())
            session.add(feedback)
            audit(session, project, owner, "business_feedback_added", task_id)
            session.flush()
            return public_feedback(feedback), True
