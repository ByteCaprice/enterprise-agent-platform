"""Independent persistence composition for the new platform surface.

This store exposes platform capabilities, governance, tasks, model/tool control
and call evidence, audit and API keys through explicit repositories.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from infra.store.repository.admission import AdmissionRepository
from infra.store.repository.agents import AgentRepository
from infra.store.repository.api_keys import ApiKeyConfigRepository
from infra.store.repository.callbacks import CallbackRepository
from infra.store.repository.capability import CapabilityRepository
from infra.store.repository.data_retention import DataRetentionRepository
from infra.store.repository.execution_queries import ExecutionQueryRepository
from infra.store.repository.gateway_requests import GatewayRequestRepository
from infra.store.repository.governance import GovernanceRepository
from infra.store.repository.log import LogRepository
from infra.store.repository.memory import MemoryRepository
from infra.store.repository.models import ModelRepository
from infra.store.repository.oidc import OIDCRepository
from infra.store.repository.operations import OperationsRepository
from infra.store.repository.quality import QualityRepository
from infra.store.repository.task import TaskRepository
from infra.store.repository.task_completion import TaskCompletionRepository
from infra.store.repository.task_control import TaskControlRepository
from infra.store.repository.task_events import TaskEventRepository
from infra.store.repository.task_queue import TaskQueueRepository
from infra.store.repository.tools import ToolRepository
from infra.store.repository.worker_health import WorkerHealthRepository
from infra.store.repository.workflow import WorkflowRepository


class PlatformStore:
    """UnitOfWork container for new-platform persistence only."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sf = sessionmaker(bind=engine, expire_on_commit=False)

        self.capabilities = CapabilityRepository(self._sf)
        self.governance = GovernanceRepository(self._sf)
        self.gateway_requests = GatewayRequestRepository(self._sf)
        self.tasks = TaskRepository(self._sf)
        self.admission = AdmissionRepository(self._sf, self.tasks, self.gateway_requests)
        self.quality = QualityRepository(self._sf)
        self.task_queue = TaskQueueRepository(self._sf)
        self.task_completion = TaskCompletionRepository(self._sf)
        self.task_control = TaskControlRepository(self._sf)
        self.task_events = TaskEventRepository(self._sf)
        self.models = ModelRepository(self._sf)
        self.operations = OperationsRepository(self._sf)
        self.worker_health = WorkerHealthRepository(self._sf)
        self.memory = MemoryRepository(self._sf)
        self.callbacks = CallbackRepository(self._sf)
        self.data_retention = DataRetentionRepository(self._sf)
        self.oidc = OIDCRepository(self._sf)
        self.agents = AgentRepository(self._sf)
        self.execution_queries = ExecutionQueryRepository(self._sf)
        self.tool_bindings = ToolRepository(self._sf)
        self.workflows = WorkflowRepository(self._sf)
        self.logs = LogRepository(self._sf)
        self.api_key_configs = ApiKeyConfigRepository(self._sf)

    @contextmanager
    def unit_of_work(self) -> Iterator[Any]:
        """Yield a transactional SQLAlchemy session shared by repositories."""
        with self._sf.begin() as session:
            yield session

    def close(self) -> None:
        self.engine.dispose()
