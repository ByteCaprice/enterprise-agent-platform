"""Owner/capability-scoped immutable memory revisions and explicit tombstones."""

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from domain.task import TaskConflict, TaskNotFound
from infra.store.model import AiCapability, AiTask
from infra.store.model.ai_operations import AiMemoryFact
from infra.store.repository.base import BaseRepository
from infra.store.repository.limits import project_lock
from infra.store.repository.operations import audit, record


class MemoryRepository(BaseRepository):
    def write(self, project, owner, spec):
        with self._sf.begin() as session:
            project_lock(session, project)
            if not session.scalar(select(AiCapability.id).where(AiCapability.project_key == project, AiCapability.key == spec.capability_key)):
                raise TaskNotFound("Memory capability not found")
            latest = session.scalar(select(AiMemoryFact).where(
                AiMemoryFact.project_key == project, AiMemoryFact.owner == owner,
                AiMemoryFact.capability_key == spec.capability_key, AiMemoryFact.subject_key == spec.subject_key,
                AiMemoryFact.fact_key == spec.fact_key).order_by(AiMemoryFact.revision.desc()).limit(1))
            if (latest.revision if latest else 0) != spec.expected_revision:
                raise TaskConflict("Memory revision changed")
            if spec.source_task_id:
                source = session.get(AiTask, spec.source_task_id)
                capability = session.get(AiCapability, source.capability_id) if source else None
                if (source is None or capability is None or source.project_key != project or source.submitted_by != owner or
                        capability.key != spec.capability_key or source.status != "succeeded"):
                    raise TaskNotFound("Succeeded owner/source Task not found")
            now = session.scalar(select(func.clock_timestamp()))
            if spec.expires_at is not None and spec.expires_at <= now:
                raise TaskConflict("Memory expiry must be in the future")
            row = AiMemoryFact(project_key=project, owner=owner, revision=spec.expected_revision + 1,
                               create_time=now, **spec.model_dump(exclude={"expected_revision"}))
            session.add(row)
            audit(session, project, owner, "memory_revision_written", f"{spec.capability_key}:{spec.subject_key}:{spec.fact_key}")
            session.flush()
            return record(row)

    def read(self, project, owner, capability, subject):
        with self._read() as session:
            newer = aliased(AiMemoryFact)
            is_latest = ~select(newer.revision).where(newer.project_key == AiMemoryFact.project_key,
                newer.owner == AiMemoryFact.owner, newer.capability_key == AiMemoryFact.capability_key,
                newer.subject_key == AiMemoryFact.subject_key, newer.fact_key == AiMemoryFact.fact_key,
                newer.revision > AiMemoryFact.revision).exists()
            rows = session.scalars(select(AiMemoryFact).where(AiMemoryFact.project_key == project,
                AiMemoryFact.owner == owner, AiMemoryFact.capability_key == capability,
                AiMemoryFact.subject_key == subject, is_latest).order_by(AiMemoryFact.fact_key).limit(200)).all()
            now = session.scalar(select(func.clock_timestamp()))
            # Tombstones/expired facts expose only the revision needed for optimistic writes.
            return [record(row) if row.state == "active" and (row.expires_at is None or row.expires_at > now)
                    else {"fact_key": row.fact_key, "revision": row.revision,
                          "state": "deleted" if row.state == "deleted" else "expired"} for row in rows]
