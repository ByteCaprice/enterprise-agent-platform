"""Application service for non-destructive retention inventory reads."""

from datetime import datetime

from domain.data_retention import RetentionInventory
from infra.store.repository.data_retention import DataRetentionRepository


class DataRetentionService:
    def __init__(self, repository: DataRetentionRepository) -> None:
        self._repository = repository

    def inventory(self, project_key: str, before: datetime) -> RetentionInventory:
        return self._repository.inventory(project_key, before)
