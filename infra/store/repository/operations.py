"""Operations administration and explicit budget reconciliation."""

from sqlalchemy import func, select

from domain import LogEvent
from domain.task import TaskConflict, TaskNotFound
from infra.store.model import AiCapability, AiRunLog, AiTask
from infra.store.model.ai_operations import (
    AiBudgetCharge,
    AiBudgetCycle,
    AiCapabilityConcurrency,
    AiGlobalConcurrency,
    AiProjectLimits,
)
from infra.store.repository.base import BaseRepository
from infra.store.repository.limits import budget_used, capability_running_count, concurrency_lock, project_lock


def record(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def audit(session, project, actor, event, resource):
    session.add(AiRunLog.from_domain(LogEvent(trace_id=f"project:{project}", component="operations_audit",
        event_type=event, message=event.replace("_", " "),
        data={"project_key": project, "actor": actor, "resource": resource})))


class OperationsRepository(BaseRepository):
    def global_concurrency(self, spec=None, actor=None):
        with self._sf.begin() as session:
            concurrency_lock(session)
            row = session.get(AiGlobalConcurrency, "global")
            if spec is not None:
                if row is None:
                    row = AiGlobalConcurrency(key="global")
                    session.add(row)
                row.max_running = spec.max_running
                audit(session, "platform", actor, "global_concurrency_updated", "global")
                session.flush()
            running = session.scalar(select(func.count()).select_from(AiTask).where(AiTask.status == "running"))
            return {"max_running": row.max_running if row else None, "running": running}

    def capability_concurrency(self, project, capability_key, spec=None, actor=None):
        with self._sf.begin() as session:
            concurrency_lock(session)
            project_lock(session, project)
            capability = session.scalar(select(AiCapability).where(
                AiCapability.project_key == project, AiCapability.key == capability_key,
            ))
            if capability is None:
                raise TaskNotFound("Capability not found")
            if capability.kind not in {"agent", "workflow"}:
                raise TaskConflict("Only executable capabilities have task concurrency")
            row = session.get(AiCapabilityConcurrency, capability.id)
            if spec is not None:
                if row is None:
                    row = AiCapabilityConcurrency(capability_id=capability.id)
                    session.add(row)
                row.max_running = spec.max_running
                audit(session, project, actor, "capability_concurrency_updated", capability_key)
                session.flush()
            root, embedded = capability_running_count(session, capability.id)
            return {"capability_key": capability_key, "max_running": row.max_running if row else None,
                    "running": root + embedded, "running_tasks": root, "running_embedded_agents": embedded}

    def limits(self, project, spec=None, actor=None):
        with self._sf.begin() as session:
            project_lock(session, project)
            row = session.get(AiProjectLimits, project)
            if spec is not None:
                if spec.active_budget_key and session.get(AiBudgetCycle, (project, spec.active_budget_key)) is None:
                    raise TaskNotFound("Budget not found")
                if row is None:
                    row = AiProjectLimits(project_key=project)
                    session.add(row)
                for key, value in spec.model_dump().items():
                    setattr(row, key, value)
                audit(session, project, actor, "project_limits_updated", project)
                session.flush()
            counts = dict(session.execute(select(AiTask.status, func.count()).where(AiTask.project_key == project)
                                          .group_by(AiTask.status)).all())
            return {"configuration": record(row) if row else None, "task_counts": counts}

    def create_budget(self, project, spec, actor):
        with self._sf.begin() as session:
            project_lock(session, project)
            if session.get(AiBudgetCycle, (project, spec.key)):
                raise TaskConflict("Budget cycle is immutable; choose a new key")
            row = AiBudgetCycle(project_key=project, **spec.model_dump(exclude={"rates"}),
                                rates=[rate.model_dump() for rate in spec.rates])
            session.add(row)
            audit(session, project, actor, "budget_created", spec.key)
            session.flush()
            return record(row)

    def budgets(self, project):
        with self._read() as session:
            return [record(row) | {"used_or_reserved_microunits": int(budget_used(session, project, row.key))}
                    for row in session.scalars(select(AiBudgetCycle).where(AiBudgetCycle.project_key == project)
                                              .order_by(AiBudgetCycle.key).limit(200))]

    def charges(self, project):
        with self._read() as session:
            return [record(row) for row in session.scalars(select(AiBudgetCharge).where(
                AiBudgetCharge.project_key == project).order_by(AiBudgetCharge.call_id).limit(200))]

    def reconcile(self, project, call_id, spec, actor):
        with self._sf.begin() as session:
            project_lock(session, project)
            row = session.get(AiBudgetCharge, call_id)
            if row is None or row.project_key != project:
                raise TaskNotFound("Charge not found")
            if row.state == "reconciled" and (row.settled_microunits, row.reconciliation_ref) == (spec.amount_microunits, spec.reference):
                return record(row)
            if row.state != "unknown":
                raise TaskConflict("Only terminal unknown charges can be reconciled once")
            row.state, row.settled_microunits, row.reconciliation_ref = "reconciled", spec.amount_microunits, spec.reference
            audit(session, project, actor, "budget_charge_reconciled", call_id)
            session.flush()
            return record(row)

    def check_ready(self):
        from infra.store.model.ai_operations import (
            AiCallbackDelivery,
            AiCallbackEndpoint,
            AiMemoryFact,
            AiOIDCBinding,
            AiOIDCProvider,
            AiQualityDataset,
            AiQualityRun,
            AiTaskFeedback,
            AiTaskReview,
        )
        from infra.store.model.ai_worker_health import AiWorkerHealth

        with self._read() as session:
            for entity in (AiWorkerHealth, AiProjectLimits, AiGlobalConcurrency, AiCapabilityConcurrency, AiBudgetCycle, AiBudgetCharge, AiCallbackEndpoint, AiCallbackDelivery,
                           AiQualityDataset, AiQualityRun, AiTaskReview, AiTaskFeedback, AiMemoryFact, AiOIDCProvider, AiOIDCBinding):
                session.execute(select(entity).limit(0))
