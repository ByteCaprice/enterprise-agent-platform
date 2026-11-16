"""Task inbox persistence hooks and scoped reads; admission owns target snapshots."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import defer

from domain import utc_now
from domain.task import TaskConflict, TaskNotFound
from domain.task_execution import TaskAttemptRecord
from infra.store.model.ai_capability import AiCapability
from infra.store.model.ai_task import AiTask
from infra.store.repository.base import BaseRepository
from infra.store.repository.execution_effects import record_task_transition


class TaskRepository(BaseRepository):
    def check_ready(self):
        with self._read() as session:
            session.execute(select(AiTask).limit(0))
            from infra.store.model.ai_task_attempt import AiTaskAttempt
            from infra.store.model.ai_workflow_node import AiWorkflowNode

            session.execute(select(AiTaskAttempt).limit(0))
            session.execute(select(AiWorkflowNode).limit(0))

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
        record_task_transition(conn, row, owner, "task_submitted")
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
