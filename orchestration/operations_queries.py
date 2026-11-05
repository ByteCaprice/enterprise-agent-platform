"""Authorized, bounded operational pages with explicit query scopes."""

from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from domain.task import TaskError
from infra.store.model.ai_operations import (
    AiBudgetCharge,
    AiBudgetCycle,
    AiCallbackDelivery,
    AiCallbackEndpoint,
    AiMemoryFact,
    AiOIDCBinding,
    AiQualityDataset,
    AiQualityRun,
    AiTaskReview,
)
from infra.store.pagination import string_key_page
from infra.store.repository.limits import budget_used
from infra.store.repository.operations import record
from orchestration.task_service import TaskService

Collection = Literal["budgets", "charges", "callbacks", "deliveries", "datasets", "evaluations", "reviews", "memory", "oidc-bindings"]


class OperationsQueries:
    def __init__(self, store, auth):
        self.store, self.auth = store, auth
        self.tasks = TaskService(store, auth)

    def page(self, principal, project, collection, *, limit=50, cursor=None, capability=None, subject=None, task_id=None):
        owner = self.tasks.owner(principal)
        admin = principal.has_scope("admin")
        models = {"budgets": AiBudgetCycle, "charges": AiBudgetCharge, "callbacks": AiCallbackEndpoint,
            "deliveries": AiCallbackDelivery, "datasets": AiQualityDataset, "evaluations": AiQualityRun,
            "reviews": AiTaskReview, "memory": AiMemoryFact, "oidc-bindings": AiOIDCBinding}
        if collection not in models:
            raise TaskError("Unknown operations collection")
        model = models[collection]
        query = select(model).where(model.project_key == project)
        columns = [model.id] if hasattr(model, "id") else [model.key] if hasattr(model, "key") else []
        if collection == "evaluations":
            allowed = self.auth.allowed_resources(principal, project, "quality:run")
            if not admin:
                query = query.where(AiQualityRun.owner == owner)
            if "*" not in allowed:
                query = query.where(AiQualityRun.capability_key.in_(allowed))
        elif collection == "memory":
            if not capability or not subject:
                raise TaskError("Memory page requires capability and subject")
            self.auth.allowed_resources(principal, project, "memory:read", capability)
            newer = aliased(AiMemoryFact)
            query = query.where(AiMemoryFact.owner == owner, AiMemoryFact.capability_key == capability,
                AiMemoryFact.subject_key == subject, ~select(newer.revision).where(
                    newer.project_key == AiMemoryFact.project_key, newer.owner == AiMemoryFact.owner,
                    newer.capability_key == AiMemoryFact.capability_key, newer.subject_key == AiMemoryFact.subject_key,
                    newer.fact_key == AiMemoryFact.fact_key, newer.revision > AiMemoryFact.revision).exists())
            columns = [AiMemoryFact.fact_key]
        elif collection == "reviews":
            if not task_id:
                raise TaskError("Review page requires task_id")
            self.tasks.get(principal, project, task_id)
            query = query.where(AiTaskReview.task_id == task_id)
        else:
            # Same administrator gate as the corresponding original list API.
            if not admin:
                from domain.governance import AccessDenied

                raise AccessDenied("Administrator required")
            self.auth.allowed_resources(principal, project, "capability:read")
        if collection == "charges":
            columns = [AiBudgetCharge.call_id]
        elif collection == "datasets":
            columns = [AiQualityDataset.key, AiQualityDataset.version]
        elif collection == "oidc-bindings":
            columns = [AiOIDCBinding.provider_key, AiOIDCBinding.subject]
        with self.store.unit_of_work() as session:
            now = session.scalar(select(func.clock_timestamp()))

            def serialize(row):
                if collection == "memory" and (row.state == "deleted" or row.expires_at is not None and row.expires_at <= now):
                    return {"fact_key": row.fact_key, "revision": row.revision,
                            "state": "deleted" if row.state == "deleted" else "expired"}
                value = record(row)
                value.pop("lease_token", None)
                if collection == "budgets":
                    value["used_or_reserved_microunits"] = int(budget_used(session, project, row.key))
                return value

            return string_key_page(session, query, columns, scope=[project, collection, owner, capability, subject, task_id],
                                   limit=limit, cursor=cursor, serialize=serialize)
