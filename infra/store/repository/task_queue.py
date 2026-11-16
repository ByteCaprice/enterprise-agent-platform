"""Task queue admission, lease renewal and execution maintenance persistence."""

from uuid import uuid4

from sqlalchemy import select, text

from domain.execution_requirements import (
    ExecutionArtifact,
    ExecutionRequirementsUnavailable,
    InstalledExecutionCapabilities,
    required_execution_profiles,
)
from infra.store.model.ai_capability import AiCapability, AiCapabilityVersion
from infra.store.model.ai_governance import AiProject
from infra.store.model.ai_task import AiTask
from infra.store.repository.base import BaseRepository
from infra.store.repository.execution_convergence import converge_execution_evidence
from infra.store.repository.execution_effects import record_task_transition


class TaskQueueRepository(BaseRepository):
    @staticmethod
    def _requirements(session, task, *, lock=False):
        """Read actual stored identities and pins, optionally locking versions.

        Identity keys/kinds and published content are immutable through the API.
        Final claim inspection share-locks only version rows: locking identities
        here would invert publishers' identity-before-dependency-version order.
        Discovery takes no row locks. Both paths use only bounded pure parsing
        and database reads, never schema subprocesses or execution adapters.
        """
        def load(key=None, version=None):
            query = select(AiCapability, AiCapabilityVersion).join(
                AiCapabilityVersion, AiCapabilityVersion.capability_id == AiCapability.id,
            ).where(AiCapability.project_key == task.project_key)
            if key is None:
                query = query.where(AiCapability.id == task.capability_id, AiCapabilityVersion.version == task.version)
            else:
                query = query.where(AiCapability.key == key, AiCapabilityVersion.version == version)
            if lock:
                query = query.with_for_update(read=True, of=AiCapabilityVersion)
            result = session.execute(query.execution_options(populate_existing=True)).one_or_none()
            if result is None:
                return None
            identity, row = result
            return ExecutionArtifact(identity.id, identity.key, identity.kind, row.to_domain())

        try:
            root = load()
            if root is None:
                raise ExecutionRequirementsUnavailable("Pinned root artifact is unavailable")
            requirements = required_execution_profiles(
                root, version=task.version, content_hash=task.content_hash, resolve=load,
            )
            return root, requirements
        except (ValueError, TypeError, KeyError, RecursionError):
            # Invalid storage is not executable and must not poison the queue
            # head. Leave it queued for operator repair, without an Attempt.
            return None

    @staticmethod
    def _global_full(session):
        from infra.store.model.ai_operations import AiGlobalConcurrency

        global_limit = session.get(AiGlobalConcurrency, "global")
        if global_limit is None or global_limit.max_running is None:
            return False
        running = session.scalar(text("SELECT count(*) FROM ai_task WHERE status='running'"))
        return running >= global_limit.max_running

    @staticmethod
    def _eligible(capabilities, *columns):
        """Advisory discovery filters, repeated under final admission locks."""
        return (
            select(*columns)
            .join(AiCapability)
            .join(AiProject, AiProject.key == AiTask.project_key)
            .where(AiTask.status == "queued", AiCapability.kind.in_(capabilities.executor_kinds))
            .where(AiProject.enabled.is_(True))
            .where(text("""NOT EXISTS (SELECT 1 FROM ai_project_limits l
                WHERE l.project_key=ai_task.project_key AND l.max_running IS NOT NULL
                AND (SELECT count(*) FROM ai_task running WHERE running.project_key=ai_task.project_key
                     AND running.status='running') >= l.max_running)"""))
            .where(text("""NOT EXISTS (SELECT 1 FROM ai_capability_concurrency l
                WHERE l.capability_id=ai_task.capability_id AND l.max_running IS NOT NULL
                AND (SELECT count(*) FROM ai_task running WHERE running.capability_id=ai_task.capability_id
                     AND running.status='running') +
                    (SELECT count(*) FROM ai_agent_execution a WHERE a.agent_capability_id=ai_task.capability_id
                     AND a.parent_node_id IS NOT NULL AND a.status='running') >= l.max_running)"""))
        )

    def _claim_candidate(self, candidate, worker_id, capabilities, lease_seconds):
        """One short write transaction; never retain a skipped candidate's locks.

        The global concurrency lock and project-before-Task order are unchanged.
        Return a saturation flag to avoid rescanning when the global limit fills
        between advisory discovery and authoritative admission.
        """
        from domain.task_execution import TaskClaim, TaskLease
        from infra.store.model.ai_task_attempt import AiTaskAttempt
        from infra.store.repository.limits import admit_claim, concurrency_lock

        with self._sf.begin() as session:
            concurrency_lock(session)
            if self._global_full(session):
                return None, True
            project = session.scalar(select(AiProject).where(
                AiProject.key == candidate.project_key, AiProject.enabled.is_(True),
            ).with_for_update(skip_locked=True))
            if project is None:
                return None, False
            selected = session.scalar(self._eligible(capabilities, AiTask).where(
                AiTask.id == candidate.id, AiTask.project_key == project.key,
            ).with_for_update(skip_locked=True, of=AiTask).execution_options(populate_existing=True))
            if selected is None or not admit_claim(session, selected.project_key):
                return None, False
            # Rebuild requirements from the locked Task's actual stored pins.
            # No detached discovery evidence authorizes this mutation.
            inspected = self._requirements(session, selected, lock=True)
            if inspected is None or not capabilities.supports(inspected[1]):
                return None, False
            root = inspected[0]
            artifact = root.artifact
            attempt_id, token = uuid4().hex, uuid4().hex + uuid4().hex
            # Database clock is authoritative for lease boundaries.
            timing = session.execute(
                text("SELECT clock_timestamp() AS now, clock_timestamp() + (:seconds * interval '1 second') AS expiry"),
                {"seconds": lease_seconds},
            ).one()
            selected.status = "running"
            attempt_num = (
                session.scalar(
                    select(AiTaskAttempt.number)
                    .where(AiTaskAttempt.task_id == selected.id)
                    .order_by(AiTaskAttempt.number.desc())
                    .limit(1)
                )
                or 0
            )
            attempt = AiTaskAttempt(
                id=attempt_id,
                task_id=selected.id,
                number=attempt_num + 1,
                worker_id=worker_id,
                token=token,
                status="running",
                start_time=timing.now,
                heartbeat_time=timing.now,
                lease_expires_at=timing.expiry,
            )
            session.add(attempt)
            record_task_transition(session, selected, worker_id, "task_claimed")
            session.flush()
            return TaskClaim(
                task=selected.to_domain(root.key, root.kind),
                lease=TaskLease(selected.id, attempt_id, worker_id, token),
                input=selected.input,
                artifact=artifact,
            ), False

    def claim(self, worker_id, *, capabilities: InstalledExecutionCapabilities, lease_seconds=30):
        from math import isfinite

        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 200:
            raise ValueError("Invalid worker_id")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not isfinite(lease_seconds)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        if not isinstance(capabilities, InstalledExecutionCapabilities):
            raise ValueError("Supply an explicit installed capability manifest")
        # Unlocked, detached keyset discovery. Incompatible/corrupt heads neither
        # consume Attempts nor retain global/project/Task/artifact write locks.
        # Every call starts at the FIFO head; there is no hidden durable cursor.
        cursor = None
        while True:
            with self._read() as session:
                if self._global_full(session):
                    return None
                page = self._eligible(
                    capabilities, AiTask.id, AiTask.create_time, AiTask.project_key,
                    AiTask.capability_id, AiTask.version, AiTask.content_hash,
                ).order_by(AiTask.create_time, AiTask.id).limit(64)
                if cursor is not None:
                    created, task_id = cursor
                    page = page.where((AiTask.create_time > created) |
                                      ((AiTask.create_time == created) & (AiTask.id > task_id)))
                candidates = session.execute(page).all()
            if not candidates:
                return None
            for candidate in candidates:
                cursor = (candidate.create_time, candidate.id)
                with self._read() as session:
                    inspected = self._requirements(session, candidate)
                if inspected is None or not capabilities.supports(inspected[1]):
                    continue
                claim, global_full = self._claim_candidate(candidate, worker_id, capabilities, lease_seconds)
                if claim is not None or global_full:
                    return claim

    def heartbeat(self, lease, *, lease_seconds=30):
        from math import isfinite

        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not isfinite(lease_seconds)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        with self._sf.begin() as session:
            result = session.execute(
                text(
                    "UPDATE ai_task_attempt SET heartbeat_time=clock_timestamp(), lease_expires_at=clock_timestamp()+(:seconds * interval '1 second') WHERE id=:attempt AND task_id=:task AND worker_id=:worker AND token=:token AND status='running' AND lease_expires_at > clock_timestamp() AND EXISTS (SELECT 1 FROM ai_task WHERE id=:task AND status='running' AND cancel_request_id IS NULL)"
                ),
                {
                    "seconds": lease_seconds,
                    "attempt": lease.attempt_id,
                    "task": lease.task_id,
                    "worker": lease.worker_id,
                    "token": lease.token,
                },
            )
            return result.rowcount == 1

    def recover_expired(self, *, limit=100):
        from infra.store.model.ai_task_attempt import AiTaskAttempt

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("Invalid limit")
        with self._sf.begin() as session:
            ids = session.scalars(
                text(
                    "SELECT id FROM ai_task WHERE status='running' AND id IN (SELECT task_id FROM ai_task_attempt WHERE status='running' AND lease_expires_at <= clock_timestamp()) ORDER BY create_time,id LIMIT :limit FOR UPDATE SKIP LOCKED"
                ),
                {"limit": limit},
            ).all()
            count = 0
            for task_id in ids:
                task = session.get(AiTask, task_id)
                attempt = session.execute(
                    select(AiTaskAttempt)
                    .where(AiTaskAttempt.task_id == task_id, AiTaskAttempt.status == "running")
                    .with_for_update()
                ).scalar_one_or_none()
                if attempt is None or attempt.lease_expires_at > session.scalar(text("SELECT clock_timestamp()")):
                    continue
                now = max(session.scalar(text("SELECT clock_timestamp()")), task.create_time,
                          task.cancel_requested_at or task.create_time, attempt.cancel_observed_at or attempt.start_time)
                error = "CANCEL_LEASE_EXPIRED_RESULT_UNKNOWN" if task.cancel_request_id is not None else "LEASE_EXPIRED_RESULT_UNKNOWN"
                task.status, task.finish_time, task.error_code = "failed", now, error
                attempt.status, attempt.finish_time, attempt.error_code = "failed", now, task.error_code
                converge_execution_evidence(session, task.id, attempt.id, now, error)
                record_task_transition(session, task, "system", "task_lease_expired")
                count += 1
            return count

    def reject_disabled_project_tasks(self, *, limit=100):
        """Fail queued work for a disabled project without starting an Attempt.

        Lock the project before its tasks, as admission does. Re-enabling the
        project and rejecting its queued work therefore have a definite order.
        """
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("Invalid limit")
        with self._sf.begin() as session:
            project = session.scalar(
                select(AiProject)
                .where(AiProject.enabled.is_(False))
                .where(select(AiTask.id).where(AiTask.project_key == AiProject.key,
                                              AiTask.status == "queued").exists())
                .order_by(AiProject.key)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if project is None:
                return 0
            tasks = session.scalars(select(AiTask).where(
                AiTask.project_key == project.key, AiTask.status == "queued",
            ).order_by(AiTask.create_time, AiTask.id).limit(limit).with_for_update(skip_locked=True)).all()
            for task in tasks:
                finished = max(session.scalar(text("SELECT clock_timestamp()")), task.create_time)
                task.status = "failed"
                task.finish_time = finished
                task.error_code = "EXECUTION_NOT_AUTHORIZED"
                record_task_transition(session, task, "system", "task_failed")
            return len(tasks)
