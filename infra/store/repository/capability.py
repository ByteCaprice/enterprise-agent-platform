"""Capability publication transactions with immutable content and atomic audit."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from domain import LogEvent, utc_now
from domain.capability import (
    CapabilityConflict,
    CapabilityNotFound,
    CapabilitySpec,
    CapabilityVersionDraft,
    CapabilityVersionRecord,
)
from infra.store.model import AiCapability, AiCapabilityVersion, AiRunLog
from infra.store.repository.base import BaseRepository


class CapabilityRepository(BaseRepository):
    @staticmethod
    def _identity(session, project_key, key, *, lock=False):
        query = select(AiCapability).where(AiCapability.project_key == project_key, AiCapability.key == key)
        if lock:
            query = query.with_for_update()
        row = session.scalar(query)
        if row is None:
            raise CapabilityNotFound("Capability not found")
        return row

    @staticmethod
    def _version(session, capability_id, version, *, lock=False):
        query = select(AiCapabilityVersion).where(
            AiCapabilityVersion.capability_id == capability_id, AiCapabilityVersion.version == version
        )
        if lock:
            query = query.with_for_update()
        row = session.scalar(query)
        if row is None:
            raise CapabilityNotFound("Capability version not found")
        return row

    @staticmethod
    def _audit(session, capability_id, actor, action, **data):
        session.add(AiRunLog.from_domain(LogEvent(
            trace_id=f"capability:{capability_id}", component="capability_audit",
            event_type=action, message=action.replace("_", " "),
            data={"actor": actor, "capability_id": capability_id, **data},
        )))

    def create(self, project_key: str, spec: CapabilitySpec, actor: str) -> dict[str, Any]:
        row = AiCapability(
            id=uuid4().hex, project_key=project_key, key=spec.key, kind=spec.kind.value,
            name=spec.name, description=spec.description, created_by=actor, create_time=utc_now(),
        )
        try:
            with self._sf.begin() as session:
                session.add(row)
                self._audit(session, row.id, actor, "capability_created", project_key=project_key, key=spec.key)
                session.flush()
                return row.to_dict()
        except IntegrityError:
            raise CapabilityConflict("Capability key already exists in this project") from None

    def get(self, project_key: str, key: str) -> dict[str, Any]:
        with self._read() as session:
            return self._identity(session, project_key, key).to_dict()

    def list(self, project_key: str, *, limit: int = 100, allowed_keys: set[str] | None = None) -> list[dict[str, Any]]:
        with self._read() as session:
            query = select(AiCapability).where(AiCapability.project_key == project_key)
            if allowed_keys is not None and "*" not in allowed_keys:
                query = query.where(AiCapability.key.in_(allowed_keys))
            rows = session.scalars(query.order_by(AiCapability.key).limit(limit)).all()
            return [row.to_dict() for row in rows]

    def create_draft(
        self, project_key: str, key: str, draft: CapabilityVersionDraft, content_hash: str, actor: str
    ) -> CapabilityVersionRecord:
        try:
            with self._sf.begin() as session:
                capability = self._identity(session, project_key, key, lock=True)
                row = AiCapabilityVersion(
                    id=uuid4().hex, capability_id=capability.id, status="draft", content_hash=content_hash,
                    created_by=actor, create_time=utc_now(), **draft.model_dump(mode="json"),
                )
                session.add(row)
                self._audit(session, capability.id, actor, "capability_draft_created",
                            version=draft.version, content_hash=content_hash)
                session.flush()
                return row.to_domain()
        except IntegrityError:
            raise CapabilityConflict("Version already exists; create a new version instead of overwriting content") from None

    def get_version(self, project_key: str, key: str, version: str) -> CapabilityVersionRecord:
        with self._read() as session:
            capability = self._identity(session, project_key, key)
            return self._version(session, capability.id, version).to_domain()

    def list_versions(self, project_key: str, key: str, *, limit: int = 100) -> list[CapabilityVersionRecord]:
        with self._read() as session:
            capability = self._identity(session, project_key, key)
            rows = session.scalars(select(AiCapabilityVersion).where(AiCapabilityVersion.capability_id == capability.id)
                                   .order_by(AiCapabilityVersion.create_time.desc()).limit(limit)).all()
            return [row.to_domain() for row in rows]

    def publish(self, project_key: str, key: str, version: str, actor: str) -> CapabilityVersionRecord:
        with self._sf.begin() as session:
            capability = self._identity(session, project_key, key, lock=True)
            row = self._version(session, capability.id, version, lock=True)
            if row.status == "published":
                return row.to_domain()
            if row.status != "draft":
                raise CapabilityConflict("Only draft versions can be published")
            for dependency in row.dependencies:
                if dependency["key"] == key:
                    raise CapabilityConflict("A capability cannot depend on itself")
                target = self._identity(session, project_key, dependency["key"])
                target_version = self._version(session, target.id, dependency["version"])
                if target_version.status != "published":
                    raise CapabilityConflict("All dependencies must reference published versions")
            row.status = "published"
            row.published_by = actor
            row.publish_time = utc_now()
            if capability.default_version is None:
                capability.default_version = version
            self._audit(session, capability.id, actor, "capability_published",
                        version=version, content_hash=row.content_hash)
            session.flush()
            return row.to_domain()

    def set_default(self, project_key: str, key: str, version: str, actor: str) -> dict[str, Any]:
        with self._sf.begin() as session:
            capability = self._identity(session, project_key, key, lock=True)
            row = self._version(session, capability.id, version)
            if row.status != "published":
                raise CapabilityConflict("Default version must be published")
            previous = capability.default_version
            capability.default_version = version
            self._audit(session, capability.id, actor, "capability_default_changed", previous=previous, version=version)
            session.flush()
            return capability.to_dict()
