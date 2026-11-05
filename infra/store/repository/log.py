"""Platform audit persistence and bounded trace/component queries."""

from typing import Any

from sqlalchemy import select

from domain import LogEvent
from infra.store.model import AiRunLog
from infra.store.repository.base import BaseRepository, _redact


class LogRepository(BaseRepository):
    def add(self, event: LogEvent, *, conn: Any | None = None) -> LogEvent:
        event.data = _redact(event.data)
        with self._write(conn) as session:
            session.add(AiRunLog.from_domain(event))
        return event

    def for_trace(self, trace_id: str, limit: int = 100) -> list[LogEvent]:
        with self._read() as session:
            entities = session.scalars(select(AiRunLog).where(AiRunLog.trace_id == trace_id)
                                       .order_by(AiRunLog.create_time.asc()).limit(limit))
            return [entity.to_domain() for entity in entities]

    def list_by_component(self, component: str, limit: int = 100) -> list[LogEvent]:
        with self._read() as session:
            entities = session.scalars(select(AiRunLog).where(AiRunLog.component == component)
                                       .order_by(AiRunLog.create_time.desc()).limit(limit))
            return [entity.to_domain() for entity in entities]
