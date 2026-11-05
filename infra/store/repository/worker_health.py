"""Advisory DB-clock heartbeats and metadata-only operational health summaries."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, text, update

from domain.task import TaskNotFound
from infra.store.model import AiProject, AiTask, AiTaskAttempt
from infra.store.model.ai_operations import AiBudgetCharge, AiCallbackDelivery
from infra.store.model.ai_worker_health import AiWorkerHealth
from infra.store.repository.base import BaseRepository
from infra.store.repository.operations import record


class WorkerHealthRepository(BaseRepository):
    def start(self, worker_id, role, executors, workflow_agents=False):
        instance = uuid4().hex
        with self._sf.begin() as session:
            now = session.scalar(select(func.clock_timestamp()))
            session.add(AiWorkerHealth(instance_id=instance, worker_id=worker_id, role=role,
                executors=list(executors), workflow_agents=workflow_agents, state="running",
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
            return [record(row) for row in session.scalars(query.order_by(AiWorkerHealth.instance_id))]

    def summary(self, project, *, stale_seconds=45, queue_warning_seconds=300):
        with self._read() as session:
            if session.get(AiProject, project) is None:
                raise TaskNotFound("Project not found")
            now = session.scalar(select(func.clock_timestamp()))
            workers = [record(row) for row in session.scalars(select(AiWorkerHealth).where(
                AiWorkerHealth.state == "running", AiWorkerHealth.heartbeat_at > now - timedelta(seconds=stale_seconds))
                .order_by(AiWorkerHealth.instance_id).limit(1000))]
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
            # Required roles derive from actual pending work; heartbeat never grants
            # authorization or substitutes for the Task/Attempt lease.
            needs = session.execute(text("""
                SELECT DISTINCT CASE WHEN c.kind='workflow' AND jsonb_path_exists(v.definition,
                    '$.nodes[*] ? (@.kind == "agent")') THEN 'workflow_agent' ELSE c.kind END AS kind
                FROM ai_task t JOIN ai_capability c ON c.id=t.capability_id
                JOIN ai_capability_version v ON v.capability_id=c.id AND v.version=t.version
                JOIN ai_project p ON p.key=t.project_key
                WHERE t.project_key=:project AND t.status='queued' AND p.enabled
            """), {"project": project}).scalars().all()
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
            if missing:
                alerts.append({"code": "WORKER_MISSING", "roles": missing})
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
                "worker_list_limit": 1000, "heartbeat_is_advisory": True}
