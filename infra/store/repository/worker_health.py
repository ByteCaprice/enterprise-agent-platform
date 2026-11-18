"""Advisory DB-clock heartbeats and metadata-only operational health summaries."""

from collections import Counter
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, text, update

from domain.task import TaskNotFound
from infra.store.model import AiProject, AiTask, AiTaskAttempt
from infra.store.model.ai_operations import AiBudgetCharge, AiCallbackDelivery
from infra.store.model.ai_worker_health import AiWorkerHealth
from infra.store.repository.base import BaseRepository
from infra.store.repository.operations import record
from infra.store.repository.worker_readiness import (
    QUEUE_SCAN_LIMIT,
    executor_names,
    heartbeat_executors,
    queued_readiness,
)

WORKER_LIST_LIMIT = 1000


def _public_worker(worker):
    return worker | {"executors": executor_names(worker["executors"])}


class WorkerHealthRepository(BaseRepository):
    def start(self, worker_id, role, executors, workflow_agents=False, *, capabilities=None):
        instance = uuid4().hex
        payload = heartbeat_executors(executors, capabilities)
        with self._sf.begin() as session:
            now = session.scalar(select(func.clock_timestamp()))
            session.add(AiWorkerHealth(instance_id=instance, worker_id=worker_id, role=role,
                executors=payload, workflow_agents=workflow_agents, state="running",
                started_at=now, heartbeat_at=now))
        return instance

    def heartbeat(self, instance, state="running"):
        with self._sf.begin() as session:
            result = session.execute(update(AiWorkerHealth).where(AiWorkerHealth.instance_id == instance,
                AiWorkerHealth.state == "running").values(state=state, heartbeat_at=func.clock_timestamp()))
            if result.rowcount != 1:
                raise RuntimeError("Worker health instance is no longer active")

    def live(self, *, stale_seconds=45, worker_id=None):
        with self._read() as session:
            now = session.scalar(select(func.clock_timestamp()))
            query = select(AiWorkerHealth).where(AiWorkerHealth.state == "running",
                AiWorkerHealth.heartbeat_at > now - timedelta(seconds=stale_seconds))
            if worker_id is not None:
                query = query.where(AiWorkerHealth.worker_id == worker_id)
            return [_public_worker(record(row)) for row in session.scalars(query.order_by(AiWorkerHealth.instance_id))]

    def summary(self, project, *, stale_seconds=45, queue_warning_seconds=300):
        with self._read() as session:
            project_record = session.get(AiProject, project)
            if project_record is None:
                raise TaskNotFound("Project not found")
            now = session.scalar(select(func.clock_timestamp()))
            observed_workers = [record(row) for row in session.scalars(select(AiWorkerHealth).where(
                AiWorkerHealth.state == "running", AiWorkerHealth.heartbeat_at > now - timedelta(seconds=stale_seconds))
                .order_by(AiWorkerHealth.instance_id).limit(WORKER_LIST_LIMIT + 1))]
            workers_truncated = len(observed_workers) > WORKER_LIST_LIMIT
            observed_workers = observed_workers[:WORKER_LIST_LIMIT]
            workers = [_public_worker(worker) for worker in observed_workers]
            task_counts = dict(session.execute(select(AiTask.status, func.count()).where(AiTask.project_key == project)
                                              .group_by(AiTask.status)).all())
            oldest = session.scalar(select(func.min(AiTask.create_time)).where(AiTask.project_key == project, AiTask.status == "queued"))
            age = max(0.0, (now - oldest).total_seconds()) if oldest else 0.0
            expired = session.scalar(select(func.count()).select_from(AiTaskAttempt).join(AiTask).where(
                AiTask.project_key == project, AiTask.status == "running", AiTaskAttempt.status == "running",
                AiTaskAttempt.lease_expires_at <= now))
            errors = dict(session.execute(select(AiTask.error_code, func.count()).where(AiTask.project_key == project,
                AiTask.error_code.in_(["LEASE_EXPIRED_RESULT_UNKNOWN", "CANCEL_LEASE_EXPIRED_RESULT_UNKNOWN",
                                      "CANCEL_CLEANUP_RESULT_UNKNOWN"])).group_by(AiTask.error_code)).all())
            unconfirmed = session.scalar(select(func.count()).select_from(AiTask).where(AiTask.project_key == project,
                AiTask.cancel_request_id.is_not(None),
                AiTask.status.in_(["failed", "timed_out"])))
            unknown = session.scalar(select(func.count()).select_from(AiBudgetCharge).where(
                AiBudgetCharge.project_key == project, AiBudgetCharge.state == "unknown"))
            deliveries = dict(session.execute(select(AiCallbackDelivery.state, func.count()).where(
                AiCallbackDelivery.project_key == project).group_by(AiCallbackDelivery.state)).all())
            # Preserve the coarse role warning, but use the same FIFO limit as
            # the exact diagnostic rather than inspecting every queued version.
            needs = []
            if project_record.enabled:
                needs = session.execute(text("""
                    SELECT DISTINCT CASE WHEN c.kind='workflow' AND jsonb_path_exists(v.definition,
                        '$.nodes[*] ? (@.kind == "agent")') THEN 'workflow_agent' ELSE c.kind END AS kind
                    FROM (SELECT capability_id, version FROM ai_task WHERE project_key=:project AND status='queued'
                          ORDER BY create_time, id LIMIT :limit) t
                    JOIN ai_capability c ON c.id=t.capability_id
                    JOIN ai_capability_version v ON v.capability_id=c.id AND v.version=t.version
                """), {"project": project, "limit": QUEUE_SCAN_LIMIT}).scalars().all()
            pending_callbacks = session.scalar(text("""
                SELECT count(*) FROM ai_callback_delivery d
                JOIN ai_callback_endpoint e ON e.project_key=d.project_key AND e.key=d.endpoint_key
                JOIN ai_project p ON p.key=d.project_key
                WHERE d.project_key=:project AND d.state IN ('pending','leased') AND e.enabled AND p.enabled
            """), {"project": project})
            if pending_callbacks:
                needs.append("callback")
            missing = []
            for kind in sorted(set(needs)):
                if not any((w["role"] == "callback" if kind == "callback" else
                    w["role"] == "task" and ("workflow" if kind == "workflow_agent" else kind) in w["executors"]
                    and (kind != "workflow_agent" or w["workflow_agents"])) for w in workers):
                    missing.append(kind)
            alerts = []
            if missing and not workers_truncated:
                alerts.append({"code": "WORKER_MISSING", "roles": missing})
            readiness = queued_readiness(session, project, enabled=project_record.enabled,
                                         workers=observed_workers, workers_truncated=workers_truncated)
            issues = Counter(issue["reason"] for issue in readiness["issues"])
            for code, count in (
                ("QUEUED_TASK_NO_COMPATIBLE_WORKER", issues["no_compatible_worker"]),
                ("QUEUED_TASK_REQUIREMENTS_UNAVAILABLE", issues["requirements_unavailable"]),
                ("QUEUED_TASK_COMPATIBILITY_UNKNOWN", issues["worker_coverage_unknown"] + issues["inspection_limit"]),
            ):
                if count:
                    alerts.append({"code": code, "count": count})
            for code, count in (("QUEUE_WAIT_EXCEEDED", int(age >= queue_warning_seconds and oldest is not None)),
                                ("TASK_LEASE_EXPIRED", expired), ("CANCELLATION_UNCONFIRMED", unconfirmed),
                                ("BUDGET_USAGE_UNKNOWN", unknown), ("CALLBACK_DEAD", deliveries.get("dead", 0))):
                if count:
                    alerts.append({"code": code, "count": count})
            return {"observed_at": now, "project_key": project, "status": "attention" if alerts else "ok",
                "task_counts": task_counts, "oldest_queued_age_seconds": age,
                "expired_attempts": expired, "historical_lease_failures": errors,
                "unconfirmed_cancellations": unconfirmed, "unknown_charges": unknown,
                "callback_counts": deliveries, "live_workers": workers, "alerts": alerts,
                "queue_readiness": readiness, "worker_list_limit": WORKER_LIST_LIMIT,
                "worker_list_truncated": workers_truncated, "heartbeat_is_advisory": True}
