"""Explicit persistence effects composed inside the caller's fenced transaction.

These helpers neither authorize transitions nor open/commit transactions. Callers
own the Task lock and admission/settlement fence; failures roll back evidence,
accounting and delivery together with the originating write.
"""

from domain import LogEvent
from domain.task_event import TERMINAL_TASK_EVENTS
from infra.store.model import AiRunLog
from infra.store.repository.callbacks import enqueue_terminal_callbacks
from infra.store.repository.limits import (
    close_task_budget_reservations,
    reserve_model_call_budget,
    settle_model_call_budget,
)
from infra.store.repository.task_events import append_task_event


def record_task_transition(session, task, actor, event_type):
    """Audit and journal a Task transition, explicitly closing terminal effects."""
    session.add(AiRunLog.from_domain(LogEvent(
        trace_id=f"task:{task.id}", component="task_audit", event_type=event_type,
        message=event_type.replace("_", " "),
        data={"actor": actor, "project_key": task.project_key, "task_id": task.id},
    )))
    sequence = append_task_event(session, task.id, event_type)
    if event_type in TERMINAL_TASK_EVENTS:
        close_task_budget_reservations(session, task.id)
        enqueue_terminal_callbacks(session, task.id, sequence, event_type)
    return sequence


def record_model_audit(session, project_key, task_id, call_id, actor, event_type):
    """Append model/provider audit only; no accounting, journal or delivery effects."""
    session.add(AiRunLog.from_domain(LogEvent(
        trace_id=f"task:{task_id or 'model-provider'}", component="model_audit", event_type=event_type,
        message=event_type.replace("_", " "),
        data={"project_key": project_key, "task_id": task_id, "call_id": call_id, "actor": actor},
    )))


def record_model_call_started(session, project_key, task_id, call_id, actor):
    """Reserve budget, append audit and journal for an admitted model call."""
    reserve_model_call_budget(session, call_id)
    record_model_audit(session, project_key, task_id, call_id, actor, "model_call_started")
    return append_task_event(session, task_id, "model_call_started", entity_kind="model_call", entity_id=call_id)


def record_model_call_settled(session, project_key, task_id, call_id, actor, event_type):
    """Settle retained Usage, append audit and journal for a fenced call result."""
    if event_type not in {"model_call_succeeded", "model_call_failed", "model_call_cancelled"}:
        raise ValueError("Invalid model settlement event")
    settle_model_call_budget(session, call_id)
    record_model_audit(session, project_key, task_id, call_id, actor, event_type)
    return append_task_event(session, task_id, event_type, entity_kind="model_call", entity_id=call_id)


def record_tool_audit(session, project, actor, event_type, *, task_id=None, call_id=None, binding_key=None):
    """Append tool/binding audit only."""
    session.add(AiRunLog.from_domain(LogEvent(
        trace_id=f"task:{task_id}" if task_id else f"project:{project}", component="tool_audit",
        event_type=event_type, message=event_type.replace("_", " "),
        data={"project_key": project, "actor": actor, "task_id": task_id,
              "call_id": call_id, "binding_key": binding_key},
    )))


def record_tool_call_transition(session, project, actor, event_type, *, task_id, call_id, binding_key=None):
    """Append audit and journal for an already-fenced tool call write."""
    record_tool_audit(session, project, actor, event_type, task_id=task_id, call_id=call_id, binding_key=binding_key)
    return append_task_event(session, task_id, event_type, entity_kind="tool_call", entity_id=call_id)
