"""Snapshot-consistent, projection-only retention inventory queries."""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from domain.data_retention import RetentionCutoffAfterReadTime, RetentionInventory, RetentionProjectNotFound
from infra.store.model.ai_gateway_request import AiGatewayRequest
from infra.store.model.ai_governance import AiProject
from infra.store.model.ai_operations import AiBudgetCharge, AiCallbackDelivery, AiMemoryFact
from infra.store.model.ai_task import AiTask


class DataRetentionRepository:
    def __init__(self, session_factory: sessionmaker[Any]) -> None:
        self._session_factory = session_factory

    def inventory(self, project_key: str, before: datetime) -> RetentionInventory:
        with self._session_factory() as session:
            with session.begin():
                # Set isolation before the first query; all subsequent projections share one snapshot.
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                session.execute(text("SET TRANSACTION READ ONLY"))
                read_time = session.scalar(select(func.transaction_timestamp()))
                if read_time is None:
                    raise RuntimeError("Database transaction timestamp unavailable")

                exists = session.scalar(select(AiProject.key).where(AiProject.key == project_key))
                if exists is None:
                    raise RetentionProjectNotFound(project_key)
                if before > read_time:
                    raise RetentionCutoffAfterReadTime(project_key)

                task_row = session.execute(
                    select(
                        func.count().label("total"),
                        func.count().filter(AiTask.status.in_(("queued", "running"))).label("nonterminal"),
                    ).where(AiTask.project_key == project_key, AiTask.create_time < before)
                ).one()
                gateway_count = session.scalar(
                    select(func.count()).select_from(AiGatewayRequest).where(
                        AiGatewayRequest.project_key == project_key, AiGatewayRequest.create_time < before
                    )
                )
                memory_count = session.scalar(
                    select(func.count()).select_from(AiMemoryFact).where(
                        AiMemoryFact.project_key == project_key,
                        AiMemoryFact.create_time < before,
                        AiMemoryFact.expires_at.is_not(None),
                        AiMemoryFact.expires_at <= read_time,
                        AiMemoryFact.state == "active",
                    )
                )
                budget_counts = session.execute(
                    select(
                        func.count().label("all_project"),
                        func.count().filter(AiBudgetCharge.state == "unknown").label("unknown"),
                    ).where(AiBudgetCharge.project_key == project_key)
                ).one()
                callbacks = session.execute(
                    select(
                        func.count().filter(AiCallbackDelivery.state == "pending").label("pending"),
                        func.count().filter(AiCallbackDelivery.state == "leased").label("leased"),
                        func.count().filter(AiCallbackDelivery.state == "dead").label("dead"),
                    ).where(AiCallbackDelivery.project_key == project_key)
                ).one()
                return RetentionInventory(
                    project_key=project_key,
                    before=before,
                    read_time=read_time,
                    tasks_created_before_count=task_row.total,
                    tasks_created_before_nonterminal_count=task_row.nonterminal,
                    gateway_requests_created_before_count=gateway_count or 0,
                    memory_revisions_expired_as_of_read_time_and_created_before_count=memory_count or 0,
                    budget_charges_temporal_scope="all_project_no_timestamp",
                    budget_charges_all_project_count=budget_counts.all_project,
                    budget_charges_all_project_unknown_count=budget_counts.unknown,
                    callback_deliveries_pending_count=callbacks.pending,
                    callback_deliveries_leased_count=callbacks.leased,
                    callback_deliveries_dead_count=callbacks.dead,
                )
