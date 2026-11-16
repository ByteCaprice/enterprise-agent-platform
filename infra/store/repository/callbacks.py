"""Metadata-only terminal outbox, independently leased at-least-once delivery."""

import base64
import binascii
import hashlib
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select, text, tuple_

from domain.callback_delivery import CallbackDeliveryPage, CallbackDeliveryView
from domain.task import TaskConflict, TaskError, TaskNotFound, canonical_json
from domain.task_event import TERMINAL_TASK_EVENTS
from infra.store.model import AiProject, AiTask
from infra.store.model.ai_operations import AiCallbackDelivery, AiCallbackEndpoint
from infra.store.repository.base import BaseRepository
from infra.store.repository.execution_queries import ExecutionQueryRepository
from infra.store.repository.limits import project_lock
from infra.store.repository.operations import audit, record


class _DeliveryCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    scope: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(strict=True, gt=0, le=2**63 - 1)
    id: str = Field(pattern=r"^[0-9a-f]{32}$")


def _delivery_cursor(value, scope):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise ValueError
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = _DeliveryCursor.model_validate_json(decoded)
        if cursor.scope != scope:
            raise ValueError
        return cursor
    except (ValueError, binascii.Error, UnicodeError, ValidationError):
        raise TaskError("Invalid callback delivery cursor") from None


def enqueue_terminal_callbacks(session, task_id, sequence, event):
    """Enqueue metadata-only delivery in the caller's transaction; never account usage."""
    if event not in TERMINAL_TASK_EVENTS:
        return
    task = session.get(AiTask, task_id)
    now = session.scalar(select(func.clock_timestamp()))
    for endpoint in session.scalars(select(AiCallbackEndpoint).where(AiCallbackEndpoint.project_key == task.project_key,
                                                                    AiCallbackEndpoint.enabled.is_(True))):
        delivery_id = uuid4().hex
        session.add(AiCallbackDelivery(id=delivery_id, project_key=task.project_key, endpoint_key=endpoint.key,
            task_id=task.id, event_sequence=sequence, state="pending", attempts=0, next_attempt_at=now,
            payload={"delivery_id": delivery_id, "project_key": task.project_key, "task_id": task.id,
                     "sequence": sequence, "event_type": event, "status": task.status}))


class CallbackRepository(BaseRepository):
    def task_deliveries(self, project, task_id, *, allowed_keys, owner, limit=100, cursor=None):
        """Observe delivery independently of Task success, under live Task read scope."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise TaskError("Callback delivery limit must be between 1 and 200")
        scope = hashlib.sha256(canonical_json(
            ["task_deliveries", project, task_id, sorted(set(allowed_keys)), owner],
        ).encode()).hexdigest()
        with self._sf() as session, session.begin():
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            read_time = session.scalar(text("SELECT transaction_timestamp()"))
            ExecutionQueryRepository._task(session, project, task_id, allowed_keys, owner)
            after = _delivery_cursor(cursor, scope)
            model = AiCallbackDelivery
            query = select(*(getattr(model, field) for field in CallbackDeliveryView.model_fields)).where(
                model.project_key == project, model.task_id == task_id,
            )
            # Sequence and ID never change when a Worker retries or settles a delivery.
            if after is not None:
                query = query.where(tuple_(model.event_sequence, model.id) > tuple_(after.sequence, after.id))
            rows = session.execute(query.order_by(model.event_sequence, model.id).limit(limit + 1)).mappings().all()
            items = tuple(CallbackDeliveryView.model_validate(dict(row)) for row in rows[:limit])
            next_cursor = None
            if len(rows) > limit:
                position = _DeliveryCursor(scope=scope, sequence=items[-1].event_sequence, id=items[-1].id)
                next_cursor = base64.urlsafe_b64encode(position.model_dump_json().encode()).decode().rstrip("=")
            return CallbackDeliveryPage(items=items, next_cursor=next_cursor, read_time=read_time)

    def endpoints(self, project, spec=None, actor=None):
        with self._sf.begin() as session:
            project_lock(session, project)
            if spec:
                if session.get(AiCallbackEndpoint, (project, spec.key)):
                    raise TaskConflict("Endpoint is immutable; choose a new key")
                row = AiCallbackEndpoint(project_key=project, enabled=True, **spec.model_dump())
                session.add(row)
                audit(session, project, actor, "callback_endpoint_created", spec.key)
                session.flush()
                return record(row)
            return [record(row) for row in session.scalars(select(AiCallbackEndpoint).where(
                AiCallbackEndpoint.project_key == project).order_by(AiCallbackEndpoint.key).limit(200))]

    def enabled(self, project, key, enabled, actor):
        with self._sf.begin() as session:
            project_lock(session, project)
            row = session.get(AiCallbackEndpoint, (project, key))
            if row is None:
                raise TaskNotFound("Callback endpoint not found")
            row.enabled = enabled
            audit(session, project, actor, "callback_endpoint_updated", key)
            session.flush()
            return record(row)

    def deliveries(self, project):
        with self._read() as session:
            return [record(row) | {"lease_token": None} for row in session.scalars(select(AiCallbackDelivery).where(
                AiCallbackDelivery.project_key == project).order_by(AiCallbackDelivery.next_attempt_at.desc()).limit(200))]

    def claim(self):
        with self._sf.begin() as session:
            now = session.scalar(select(func.clock_timestamp()))
            rows = session.scalars(select(AiCallbackDelivery).where(or_(
                (AiCallbackDelivery.state == "pending") & (AiCallbackDelivery.next_attempt_at <= now),
                (AiCallbackDelivery.state == "leased") & (AiCallbackDelivery.lease_expires_at <= now)))
                .order_by(AiCallbackDelivery.next_attempt_at, AiCallbackDelivery.id).with_for_update(skip_locked=True).limit(100))
            for row in rows:
                endpoint = session.get(AiCallbackEndpoint, (row.project_key, row.endpoint_key))
                project = session.get(AiProject, row.project_key)
                if row.attempts >= endpoint.max_attempts:
                    row.state, row.lease_token, row.lease_expires_at = "dead", None, None
                    row.error_code = "CALLBACK_ATTEMPTS_EXHAUSTED_RESULT_UNKNOWN"
                    continue
                if not endpoint.enabled or not project.enabled:
                    # Pause disabled endpoints without a tight polling loop.
                    row.state, row.lease_token, row.lease_expires_at = "pending", None, None
                    row.next_attempt_at = now + timedelta(seconds=60)
                    continue
                row.state, row.attempts = "leased", row.attempts + 1
                row.lease_token, row.lease_expires_at = uuid4().hex, now + timedelta(seconds=endpoint.timeout_seconds + 15)
                session.flush()
                return record(row), record(endpoint)
            return None

    def finish(self, delivery_id, token, *, http_status=None, error_code=None):
        with self._sf.begin() as session:
            now = session.scalar(select(func.clock_timestamp()))
            row = session.scalar(select(AiCallbackDelivery).where(AiCallbackDelivery.id == delivery_id,
                AiCallbackDelivery.state == "leased", AiCallbackDelivery.lease_token == token,
                AiCallbackDelivery.lease_expires_at > now).with_for_update())
            if row is None:
                return False
            endpoint = session.get(AiCallbackEndpoint, (row.project_key, row.endpoint_key))
            row.http_status, row.error_code = http_status, error_code
            row.state = "delivered" if http_status and 200 <= http_status < 300 else (
                "dead" if row.attempts >= endpoint.max_attempts else "pending")
            row.next_attempt_at = now + timedelta(seconds=endpoint.retry_delay_seconds)
            row.lease_token, row.lease_expires_at = None, None
            audit(session, row.project_key, "callback-worker", "callback_delivery_" + row.state, row.id)
            return True
