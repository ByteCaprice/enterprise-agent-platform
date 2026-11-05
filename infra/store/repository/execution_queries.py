"""Bounded, read-only execution projections; no scheduling or runtime dependencies."""

from __future__ import annotations

import base64
import binascii
import hashlib
from contextlib import contextmanager
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text, tuple_

from domain.execution_query import (
    AgentExecutionItem,
    AgentExecutionView,
    AgentStepItem,
    AgentStepView,
    ExecutionRef,
    ExecutionTreePage,
    MetadataPage,
    ModelCallItem,
    ModelCallView,
    ToolCallItem,
    ToolCallView,
    WorkflowNodeItem,
)
from domain.task import TaskError, TaskNotFound, TaskRecord, canonical_json
from domain.task_execution import TaskAttemptRecord
from domain.workflow import WorkflowNodeRecord
from infra.store.model import (
    AiAgentExecution,
    AiAgentStep,
    AiCapability,
    AiPlatformModelCall,
    AiPlatformToolCall,
    AiTask,
    AiTaskAttempt,
    AiWorkflowNode,
)
from infra.store.repository.base import BaseRepository


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    scope: str = Field(pattern=r"^[0-9a-f]{64}$")
    rank: int = Field(strict=True, ge=0, le=4)
    time: AwareDatetime
    id: str = Field(min_length=1, max_length=200)


def _scope_key(*parts):
    return hashlib.sha256(canonical_json(list(parts)).encode()).hexdigest()


def _decode(value, scope):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise ValueError
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = _Cursor.model_validate_json(decoded)
        if cursor.scope != scope:
            raise ValueError
        return cursor
    except (ValueError, binascii.Error, UnicodeError, ValidationError):
        raise TaskError("Invalid execution query cursor") from None


def _encode(scope, rank, time, id):
    cursor = _Cursor(scope=scope, rank=rank, time=time, id=id)
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode().rstrip("=")


def _projection(model, view):
    # Never select an ORM entity or discover columns from its internal record.
    return select(*(getattr(model, field) for field in view.model_fields))


def _limit(value):
    if type(value) is not int or not 1 <= value <= 200:
        raise TaskError("Execution query limit must be between 1 and 200")
    return value


class ExecutionQueryRepository(BaseRepository):
    @contextmanager
    def _snapshot(self):
        # PostgreSQL is the platform store. All parent checks and projections in
        # this request see the same commit boundary without locking a Worker.
        with self._sf() as session, session.begin():
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            read_time = session.scalar(text("SELECT transaction_timestamp()"))
            yield session, read_time

    @staticmethod
    def _task(session, project_key, task_id, allowed_keys, owner):
        columns = [AiCapability.key.label("capability_key"), AiCapability.kind.label("capability_kind")]
        columns.extend(getattr(AiTask, name) for name in TaskRecord.model_fields
                       if name not in {"capability_key", "capability_kind"})
        query = select(*columns).select_from(AiTask).join(AiCapability, AiCapability.id == AiTask.capability_id).where(
            AiTask.project_key == project_key, AiTask.id == task_id,
        )
        if "*" not in allowed_keys:
            query = query.where(AiCapability.key.in_(allowed_keys))
        if owner is not None:
            query = query.where(AiTask.submitted_by == owner)
        row = session.execute(query).mappings().one_or_none()
        if row is None:
            raise TaskNotFound("Task not found")
        return TaskRecord.model_validate(dict(row))

    @staticmethod
    def _attempt(session, task_id, attempt_id):
        row = session.execute(_projection(AiTaskAttempt, TaskAttemptRecord).where(
            AiTaskAttempt.task_id == task_id, AiTaskAttempt.id == attempt_id,
        )).mappings().one_or_none()
        if row is None:
            raise TaskNotFound("Attempt not found")
        return TaskAttemptRecord.model_validate(dict(row))

    @staticmethod
    def _execution(session, project_key, task_id, execution_id):
        row = session.execute(_projection(AiAgentExecution, AgentExecutionView).where(
            AiAgentExecution.project_key == project_key, AiAgentExecution.task_id == task_id,
            AiAgentExecution.id == execution_id,
        )).mappings().one_or_none()
        if row is None:
            raise TaskNotFound("Agent execution not found")
        return AgentExecutionView.model_validate(dict(row))

    @staticmethod
    def _rows(session, model, view, filters, time_column, cursor, limit):
        query = _projection(model, view).where(*filters)
        if cursor is not None:
            query = query.where(tuple_(time_column, model.id) > tuple_(cursor.time, cursor.id))
        return [view.model_validate(dict(row)) for row in session.execute(
            query.order_by(time_column, model.id).limit(limit + 1),
        ).mappings()]

    def agent_executions(self, project_key, task_id, *, allowed_keys, owner, attempt_id=None, limit=100, cursor=None):
        limit = _limit(limit)
        scope = _scope_key("agent_executions", project_key, task_id, attempt_id)
        after = _decode(cursor, scope)
        if after is not None and after.rank != 0:
            raise TaskError("Invalid collection cursor")
        with self._snapshot() as (session, read_time):
            self._task(session, project_key, task_id, allowed_keys, owner)
            filters = [AiAgentExecution.project_key == project_key, AiAgentExecution.task_id == task_id]
            if attempt_id is not None:
                self._attempt(session, task_id, attempt_id)
                filters.append(AiAgentExecution.task_attempt_id == attempt_id)
            rows = self._rows(session, AiAgentExecution, AgentExecutionView, filters,
                              AiAgentExecution.start_time, after, limit)
            items = tuple(rows[:limit])
            next_cursor = _encode(scope, 0, items[-1].start_time, items[-1].id) if len(rows) > limit else None
            return MetadataPage[AgentExecutionView](items=items, next_cursor=next_cursor, read_time=read_time)

    def agent_execution(self, project_key, task_id, execution_id, *, allowed_keys, owner):
        with self._snapshot() as (session, _):
            self._task(session, project_key, task_id, allowed_keys, owner)
            return self._execution(session, project_key, task_id, execution_id)

    def agent_steps(self, project_key, task_id, execution_id, *, allowed_keys, owner, limit=100, cursor=None):
        limit = _limit(limit)
        scope = _scope_key("agent_steps", project_key, task_id, execution_id)
        after = _decode(cursor, scope)
        if after is not None and after.rank != 0:
            raise TaskError("Invalid collection cursor")
        with self._snapshot() as (session, read_time):
            self._task(session, project_key, task_id, allowed_keys, owner)
            execution = self._execution(session, project_key, task_id, execution_id)
            filters = [AiAgentStep.project_key == project_key, AiAgentStep.task_id == task_id,
                       AiAgentStep.task_attempt_id == execution.task_attempt_id,
                       AiAgentStep.agent_execution_id == execution_id]
            rows = self._rows(session, AiAgentStep, AgentStepView, filters, AiAgentStep.create_time, after, limit)
            items = tuple(rows[:limit])
            next_cursor = _encode(scope, 0, items[-1].create_time, items[-1].id) if len(rows) > limit else None
            return MetadataPage[AgentStepView](items=items, next_cursor=next_cursor, read_time=read_time)

    def agent_step(self, project_key, task_id, execution_id, step_id, *, allowed_keys, owner):
        with self._snapshot() as (session, _):
            self._task(session, project_key, task_id, allowed_keys, owner)
            execution = self._execution(session, project_key, task_id, execution_id)
            row = session.execute(_projection(AiAgentStep, AgentStepView).where(
                AiAgentStep.project_key == project_key, AiAgentStep.task_id == task_id,
                AiAgentStep.task_attempt_id == execution.task_attempt_id,
                AiAgentStep.agent_execution_id == execution_id, AiAgentStep.id == step_id,
            )).mappings().one_or_none()
            if row is None:
                raise TaskNotFound("Agent step not found")
            return AgentStepView.model_validate(dict(row))

    def tree(self, project_key, task_id, attempt_id, *, allowed_keys, owner, limit=100, cursor=None):
        limit = _limit(limit)
        scope = _scope_key("execution_tree", project_key, task_id, attempt_id)
        after = _decode(cursor, scope)
        with self._snapshot() as (session, read_time):
            task = self._task(session, project_key, task_id, allowed_keys, owner)
            attempt = self._attempt(session, task_id, attempt_id)
            streams = (
                (AiWorkflowNode, WorkflowNodeRecord, AiWorkflowNode.start_time),
                (AiAgentExecution, AgentExecutionView, AiAgentExecution.start_time),
                (AiAgentStep, AgentStepView, AiAgentStep.create_time),
                (AiPlatformModelCall, ModelCallView, AiPlatformModelCall.start_time),
                (AiPlatformToolCall, ToolCallView, AiPlatformToolCall.start_time),
            )
            # Parent kinds precede child kinds. Read only enough rows to fill the
            # page plus its lookahead; never load an entire large execution tree.
            found = []
            for rank, (model, view, time_column) in enumerate(streams):
                if after is not None and rank < after.rank:
                    continue
                filters = [model.task_id == task_id, model.task_attempt_id == attempt_id]
                if model is not AiWorkflowNode:
                    filters.append(model.project_key == project_key)
                rows = self._rows(session, model, view, filters, time_column,
                                  after if after is not None and rank == after.rank else None,
                                  limit - len(found))
                found.extend((rank, getattr(row, time_column.key), row) for row in rows)
                if len(found) > limit:
                    break
            items = tuple(self._tree_item(rank, row) for rank, _, row in found[:limit])
            next_cursor = (_encode(scope, found[limit - 1][0], found[limit - 1][1], found[limit - 1][2].id)
                           if len(found) > limit else None)
            return ExecutionTreePage(task=task, attempt=attempt, items=items,
                                     next_cursor=next_cursor, read_time=read_time)

    @staticmethod
    def _tree_item(rank, row):
        if rank == 0:
            return WorkflowNodeItem(parent=ExecutionRef(kind="attempt", id=row.task_attempt_id), record=row)
        if rank == 1:
            parent = (ExecutionRef(kind="workflow_node", id=row.parent_node_id) if row.parent_node_id is not None
                      else ExecutionRef(kind="attempt", id=row.task_attempt_id))
            return AgentExecutionItem(parent=parent, record=row)
        if rank == 2:
            return AgentStepItem(parent=ExecutionRef(kind="agent_execution", id=row.agent_execution_id), record=row)
        parent = (ExecutionRef(kind="workflow_node", id=row.node_id) if row.node_id is not None
                  else ExecutionRef(kind="agent_step", id=row.agent_step_id))
        return ModelCallItem(parent=parent, record=row) if rank == 3 else ToolCallItem(parent=parent, record=row)
