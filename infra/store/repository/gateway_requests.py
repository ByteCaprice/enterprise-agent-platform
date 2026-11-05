"""Append-only persistence and authorized reads of accepted gateway requests."""

from __future__ import annotations

import base64
import binascii
import hashlib
from contextlib import contextmanager
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, insert, or_, select, text
from sqlalchemy.orm import Session

from domain.gateway import GatewayRequestCreate, GatewayRequestPage, GatewayRequestRecord
from domain.task import TaskError, TaskNotFound, canonical_json
from infra.store.model.ai_gateway_request import AiGatewayRequest
from infra.store.model.ai_task import AiTask
from infra.store.repository.base import BaseRepository


class _RequestCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    scope: str = Field(pattern=r"^[0-9a-f]{64}$")
    time: AwareDatetime
    id: str = Field(pattern=r"^[0-9a-f]{32}$")


def _scope(project_key, task_id, allowed_keys, owner):
    keys = sorted(set(allowed_keys))
    value = canonical_json(["gateway_requests", project_key, task_id, keys, owner])
    return hashlib.sha256(value.encode()).hexdigest()


def _decode_cursor(value, scope):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise ValueError
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = _RequestCursor.model_validate_json(decoded)
        if cursor.scope != scope:
            raise ValueError
        return cursor
    except (ValueError, binascii.Error, UnicodeError, ValidationError):
        raise TaskError("Invalid gateway request cursor") from None


def _encode_cursor(scope, create_time, request_id):
    payload = _RequestCursor(scope=scope, time=create_time, id=request_id)
    return base64.urlsafe_b64encode(payload.model_dump_json().encode()).decode().rstrip("=")


class GatewayRequestRepository(BaseRepository):
    """Caller-owned writes; reads authorize against the Task in one snapshot."""

    def create(self, record: GatewayRequestCreate, *, conn: Session) -> GatewayRequestRecord:
        """Insert one immutable record; DB clock is authoritative for create_time."""
        submitted_by = conn.scalar(select(AiTask.submitted_by).where(
            AiTask.id == record.task_id, AiTask.project_key == record.project_key,
        ))
        if submitted_by != record.submitted_by:
            raise TaskNotFound("Task not found")
        values = record.model_dump()
        values["create_time"] = text("clock_timestamp()")
        row = conn.execute(insert(AiGatewayRequest).values(**values).returning(
            *(getattr(AiGatewayRequest, field) for field in GatewayRequestRecord.model_fields)
        )).mappings().one()
        return GatewayRequestRecord.model_validate(dict(row))

    def get(self, project_key, request_id, *, allowed_keys, owner) -> GatewayRequestRecord:
        from infra.store.repository.execution_queries import ExecutionQueryRepository

        with self._snapshot() as (session, _):
            task_id = session.scalar(select(AiGatewayRequest.task_id).where(
                AiGatewayRequest.project_key == project_key, AiGatewayRequest.id == request_id,
            ))
            if task_id is None:
                raise TaskNotFound("Task not found")
            ExecutionQueryRepository._task(session, project_key, task_id, allowed_keys, owner)
            row = session.execute(self._projection().where(
                AiGatewayRequest.project_key == project_key,
                AiGatewayRequest.id == request_id,
            )).mappings().one_or_none()
            if row is None:
                raise TaskNotFound("Task not found")
            return GatewayRequestRecord.model_validate(dict(row))

    def list(self, project_key, task_id, *, allowed_keys, owner, limit=100, cursor=None) -> GatewayRequestPage:
        from infra.store.repository.execution_queries import ExecutionQueryRepository

        if type(limit) is not int or not 1 <= limit <= 200:
            raise TaskError("Gateway request limit must be between 1 and 200")
        scope = _scope(project_key, task_id, allowed_keys, owner)
        with self._snapshot() as (session, read_time):
            ExecutionQueryRepository._task(session, project_key, task_id, allowed_keys, owner)
            after = _decode_cursor(cursor, scope)
            query = self._projection().where(
                AiGatewayRequest.project_key == project_key,
                AiGatewayRequest.task_id == task_id,
            )
            if after is not None:
                query = query.where(or_(
                    AiGatewayRequest.create_time > after.time,
                    and_(AiGatewayRequest.create_time == after.time, AiGatewayRequest.id > after.id),
                ))
            rows = session.execute(query.order_by(AiGatewayRequest.create_time, AiGatewayRequest.id)
                                   .limit(limit + 1)).mappings().all()
            records = tuple(GatewayRequestRecord.model_validate(dict(row)) for row in rows[:limit])
            next_cursor = (_encode_cursor(scope, records[-1].create_time, records[-1].id)
                           if len(rows) > limit else None)
            return GatewayRequestPage(items=records, next_cursor=next_cursor, read_time=read_time)

    @staticmethod
    def _projection():
        # Explicit record fields only; never load an entity or task body.
        return select(*(getattr(AiGatewayRequest, field) for field in GatewayRequestRecord.model_fields))

    @contextmanager
    def _snapshot(self):
        with self._sf() as session, session.begin():
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            read_time = session.scalar(text("SELECT transaction_timestamp()"))
            yield session, read_time

    def check_ready(self):
        with self._read() as session:
            session.execute(select(AiGatewayRequest.id).limit(0))
