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
from domain.capability_publication import PublicationEvidence
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
    def _version(session, capability_id, version, *, lock=False, shared=False):
        query = select(AiCapabilityVersion).where(
            AiCapabilityVersion.capability_id == capability_id, AiCapabilityVersion.version == version
        )
        if lock or shared:
            query = query.with_for_update(read=shared)
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

    def publish(self, project_key: str, key: str, version: str, actor: str, *,
                evidence: PublicationEvidence) -> CapabilityVersionRecord:
        with self._sf.begin() as session:
            capability = self._identity(session, project_key, key, lock=True)
            row = self._version(session, capability.id, version, lock=True)
            artifact = row.to_domain()
            artifact.verify_integrity()
            if (capability.id, capability.kind, row.version, row.content_hash) != (
                evidence.capability_id, evidence.kind, evidence.version, evidence.content_hash,
            ):
                raise CapabilityConflict("Validated publication artifact changed")
            if row.status == "published":
                return row.to_domain()
            if row.status != "draft":
                raise CapabilityConflict("Only draft versions can be published")
            validated = {(item.key, item.version): item for item in evidence.dependencies}
            declared = {(item["key"], item["version"]) for item in row.dependencies}
            if declared != set(validated) or len(evidence.dependencies) != len(row.dependencies):
                raise CapabilityConflict("Validated publication dependencies changed")
            # Stable lock order for shared dependencies across concurrent publishers.
            for reference in sorted(declared):
                proof = validated[reference]
                if proof.key == key:
                    raise CapabilityConflict("A capability cannot depend on itself")
                target = self._identity(session, project_key, proof.key)
                # Published dependencies are read-only. Exclusive locks here
                # can deadlock with a claimant traversing the same graph in a
                # different order; shared locks retain the evidence through commit.
                target_version = self._version(session, target.id, proof.version, shared=True)
                if (target.id, target.kind) != (proof.capability_id, proof.kind):
                    raise CapabilityConflict("Validated publication dependency changed")
                target_version.to_domain().require_published(
                    capability_id=proof.capability_id, version=proof.version, content_hash=proof.content_hash,
                )
            row.status = "published"
            row.published_by = actor
            row.publish_time = utc_now()
            if capability.default_version is None:
                capability.default_version = version
            self._audit(session, capability.id, actor, "capability_published",
                        version=version, content_hash=row.content_hash, interpreter_profile=evidence.interpreter_profile,
                        dependencies=[{"key": item.key, "version": item.version, "content_hash": item.content_hash}
                                      for item in evidence.dependencies])
            session.flush()
            return row.to_domain()

    def set_default(self, project_key: str, key: str, version: str, actor: str) -> dict[str, Any]:
        with self._sf.begin() as session:
            capability = self._identity(session, project_key, key, lock=True)
            row = self._version(session, capability.id, version)
            row.to_domain().require_published(capability_id=capability.id, version=version)
            previous = capability.default_version
            capability.default_version = version
            self._audit(session, capability.id, actor, "capability_default_changed", previous=previous, version=version)
            session.flush()
            return capability.to_dict()
