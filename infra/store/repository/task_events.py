"""Task-serialized atomic event append and authorized sequence reads, not log replay."""

from sqlalchemy import select, text, update

from domain.task import TaskError
from domain.task_event import TaskEventPage, TaskEventRecord
from infra.store.model import AiTask, AiTaskAttempt, AiTaskEvent
from infra.store.repository.base import BaseRepository


def append_task_event(session, task_id, event_type, *, entity_kind="task", entity_id=None, attempt_id=None):
    """Append journal evidence and return its sequence; no accounting or delivery."""
    # Writers already own the Task lock, or have just inserted the Task. Flush
    # complete records before allocating; counter + event roll back together.
    session.flush()
    head = session.execute(update(AiTask).where(AiTask.id == task_id).values(
        event_sequence=AiTask.event_sequence + 1,
    ).returning(AiTask.event_sequence, AiTask.project_key),
        execution_options={"synchronize_session": "fetch"}).one()
    if attempt_id is None:
        attempt_id = session.scalar(select(AiTaskAttempt.id).where(AiTaskAttempt.task_id == task_id)
                                    .order_by(AiTaskAttempt.number.desc()).limit(1))
    session.add(AiTaskEvent(task_id=task_id, project_key=head.project_key, sequence=head.event_sequence,
                            task_attempt_id=attempt_id, entity_kind=entity_kind, entity_id=entity_id or task_id,
                            event_type=event_type, occurred_at=session.scalar(text("SELECT clock_timestamp()"))))
    return head.event_sequence


class TaskEventRepository(BaseRepository):
    def list(self, project_key, task_id, *, allowed_keys, owner, after_sequence=0, limit=100):
        from infra.store.repository.execution_queries import ExecutionQueryRepository

        if type(limit) is not int or not 1 <= limit <= 200:
            raise TaskError("Invalid event page limit")
        if type(after_sequence) is not int or not 0 <= after_sequence <= 9007199254740991:
            raise TaskError("Invalid event position")
        with self._sf() as session, session.begin():
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            task = ExecutionQueryRepository._task(session, project_key, task_id, allowed_keys, owner)
            if after_sequence > task.event_sequence:
                raise TaskError("Event position is ahead of this Task")
            rows = session.execute(select(*(getattr(AiTaskEvent, name) for name in TaskEventRecord.model_fields)).where(
                AiTaskEvent.task_id == task_id, AiTaskEvent.project_key == project_key,
                AiTaskEvent.sequence > after_sequence, AiTaskEvent.sequence <= task.event_sequence,
            ).order_by(AiTaskEvent.sequence).limit(limit + 1)).mappings().all()
            items = tuple(TaskEventRecord.model_validate(dict(row)) for row in rows[:limit])
            return TaskEventPage(items=items, head_sequence=task.event_sequence,
                                 next_after=items[-1].sequence if items else after_sequence,
                                 has_more=len(rows) > limit, history_complete=task.event_history_complete)

    def check_ready(self):
        with self._read() as session:
            session.execute(select(AiTaskEvent.sequence).limit(0))
