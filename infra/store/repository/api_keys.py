"""Digest-backed control-plane key persistence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

from domain.api_key_config import ApiKeyConfig
from domain.ids import utc_now
from infra.store.model.ai_api_key_config import AiApiKeyConfig
from infra.store.repository.base import BaseRepository


class ApiKeyConfigRepository(BaseRepository):
    def save(self, config: ApiKeyConfig, *, conn: Any | None = None) -> ApiKeyConfig:
        incoming = AiApiKeyConfig.from_domain(config)
        incoming.last_time = utc_now()
        with self._write(conn) as session:
            self._upsert_by_key(session, incoming, ["name"])
        return config

    def get(self, name: str, *, conn: Any | None = None) -> ApiKeyConfig | None:
        context = self._write(conn) if conn is not None else self._read()
        with context as session:
            entity = session.scalar(select(AiApiKeyConfig).where(AiApiKeyConfig.name == name))
            return entity.to_domain() if entity is not None else None

    def list(self, *, conn: Any | None = None) -> list[ApiKeyConfig]:
        context = self._write(conn) if conn is not None else self._read()
        with context as session:
            return [entity.to_domain() for entity in session.scalars(select(AiApiKeyConfig).order_by(AiApiKeyConfig.name))]

    def delete(self, name: str, *, conn: Any | None = None) -> None:
        with self._write(conn) as session:
            session.execute(delete(AiApiKeyConfig).where(AiApiKeyConfig.name == name))

    def find_by_digest(self, digest: str) -> ApiKeyConfig | None:
        with self._read() as session:
            entity = session.scalar(select(AiApiKeyConfig).where(AiApiKeyConfig.api_key == digest))
            return entity.to_domain() if entity is not None else None

    def replace(self, configs: list[ApiKeyConfig], *, conn: Any | None = None) -> None:
        with self._write(conn) as session:
            session.execute(delete(AiApiKeyConfig))
            for config in configs:
                incoming = AiApiKeyConfig.from_domain(config)
                incoming.last_time = utc_now()
                session.add(incoming)
