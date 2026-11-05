"""Transactional identity administration and request-time authorization queries."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from domain import LogEvent, utc_now
from domain.governance import AccessDenied, GovernanceConflict, GovernanceNotFound
from infra.store.model import AiCapability, AiProject, AiResourceGrant, AiRunLog, AiServiceAccessKey, AiServiceIdentity
from infra.store.repository.base import BaseRepository


def public_record(row):
    # The digest is never part of an API response, even to administrators.
    return {column.name: getattr(row, column.name) for column in row.__table__.columns if column.name != "digest"}


class GovernanceRepository(BaseRepository):
    def check_ready(self):
        with self._read() as session:
            for entity in (AiServiceIdentity, AiServiceAccessKey, AiResourceGrant):
                session.execute(select(entity.id).limit(0))

    @staticmethod
    def _audit(session, actor, event_type, project_key, **data):
        session.add(AiRunLog.from_domain(LogEvent(
            trace_id=f"project:{project_key}", component="governance_audit", event_type=event_type,
            message=event_type.replace("_", " "), data={"actor": actor, "project_key": project_key, **data},
        )))

    @staticmethod
    def _project(session, project_key, *, lock=False):
        query = select(AiProject).where(AiProject.key == project_key)
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise GovernanceNotFound("Project not found")
        return row

    @staticmethod
    def _identity(session, project_key, identity_id):
        row = session.scalar(select(AiServiceIdentity).where(
            AiServiceIdentity.project_key == project_key, AiServiceIdentity.id == identity_id,
        ).with_for_update())
        if row is None:
            raise GovernanceNotFound("Service identity not found")
        return row

    def create_project(self, spec, actor):
        try:
            with self._sf.begin() as session:
                row = AiProject(**spec.model_dump(), enabled=True, created_by=actor, create_time=utc_now())
                session.add(row)
                self._audit(session, actor, "project_created", spec.key)
                session.flush()
                return public_record(row)
        except IntegrityError:
            raise GovernanceConflict("Project key already exists") from None

    def list_projects(self, *, limit=100):
        with self._read() as session:
            return [public_record(row) for row in session.scalars(select(AiProject).order_by(AiProject.key).limit(limit))]

    def get_project(self, project_key):
        with self._read() as session:
            return public_record(self._project(session, project_key))

    def set_project_enabled(self, project_key, enabled, actor):
        with self._sf.begin() as session:
            row = self._project(session, project_key, lock=True)
            row.enabled = enabled
            self._audit(session, actor, "project_enabled_changed", project_key, enabled=enabled)
            return public_record(row)

    def create_identity(self, project_key, spec, actor):
        try:
            with self._sf.begin() as session:
                project = self._project(session, project_key, lock=True)
                if not project.enabled:
                    raise AccessDenied("Project is disabled")
                row = AiServiceIdentity(id=uuid4().hex, project_key=project_key, **spec.model_dump(),
                                        enabled=True, created_by=actor, create_time=utc_now())
                session.add(row)
                self._audit(session, actor, "service_identity_created", project_key, identity_id=row.id)
                session.flush()
                return public_record(row)
        except IntegrityError:
            raise GovernanceConflict("Service identity key already exists in project") from None

    def list_identities(self, project_key, *, limit=100):
        with self._read() as session:
            self._project(session, project_key)
            return [public_record(row) for row in session.scalars(select(AiServiceIdentity).where(
                AiServiceIdentity.project_key == project_key,
            ).order_by(AiServiceIdentity.key).limit(limit))]

    def set_identity_enabled(self, project_key, identity_id, enabled, actor):
        with self._sf.begin() as session:
            self._project(session, project_key, lock=True)
            row = self._identity(session, project_key, identity_id)
            row.enabled = enabled
            self._audit(session, actor, "service_identity_enabled_changed", project_key,
                        identity_id=identity_id, enabled=enabled)
            return public_record(row)

    def issue_key(self, project_key, identity_id, spec, digest, actor):
        with self._sf.begin() as session:
            project = self._project(session, project_key, lock=True)
            identity = self._identity(session, project_key, identity_id)
            if not project.enabled or not identity.enabled:
                raise AccessDenied("Project or service identity is disabled")
            row = AiServiceAccessKey(id=uuid4().hex, project_key=project_key, identity_id=identity_id,
                                     **spec.model_dump(), digest=digest, revoked=False,
                                     created_by=actor, create_time=utc_now())
            session.add(row)
            self._audit(session, actor, "service_key_issued", project_key, identity_id=identity_id, access_key_id=row.id)
            session.flush()
            return public_record(row)

    def list_keys(self, project_key, identity_id, *, limit=100):
        with self._read() as session:
            # Read-only, project-scoped existence check (no write lock).
            if session.scalar(select(AiServiceIdentity.id).where(
                AiServiceIdentity.project_key == project_key, AiServiceIdentity.id == identity_id,
            )) is None:
                raise GovernanceNotFound("Service identity not found")
            return [public_record(row) for row in session.scalars(select(AiServiceAccessKey).where(
                AiServiceAccessKey.project_key == project_key, AiServiceAccessKey.identity_id == identity_id,
            ).order_by(AiServiceAccessKey.create_time.desc(), AiServiceAccessKey.id).limit(limit))]

    def revoke_key(self, project_key, identity_id, key_id, actor):
        with self._sf.begin() as session:
            self._project(session, project_key, lock=True)
            self._identity(session, project_key, identity_id)
            row = session.scalar(select(AiServiceAccessKey).where(
                AiServiceAccessKey.project_key == project_key, AiServiceAccessKey.identity_id == identity_id,
                AiServiceAccessKey.id == key_id,
            ).with_for_update())
            if row is None:
                raise GovernanceNotFound("Access key not found")
            if not row.revoked:
                row.revoked = True
                self._audit(session, actor, "service_key_revoked", project_key,
                            identity_id=identity_id, access_key_id=key_id)
            return public_record(row)

    def create_grant(self, project_key, identity_id, spec, actor):
        with self._sf.begin() as session:
            project = self._project(session, project_key, lock=True)
            identity = self._identity(session, project_key, identity_id)
            if not project.enabled or not identity.enabled:
                raise AccessDenied("Project or service identity is disabled")
            if spec.resource_key != "*":
                capability = session.scalar(select(AiCapability).where(
                    AiCapability.project_key == project_key, AiCapability.key == spec.resource_key,
                ))
                if capability is None:
                    raise GovernanceNotFound("Capability not found in project")
                if spec.action.value == "model:invoke" and capability.kind != "model_policy":
                    raise GovernanceConflict("Model invocation grants must target a model policy")
                if spec.action.value == "tool:invoke" and capability.kind != "tool":
                    raise GovernanceConflict("Tool invocation grants must target a tool capability")
                if spec.action.value == "agent:invoke" and capability.kind != "agent":
                    raise GovernanceConflict("Agent invocation grants must target an Agent capability")
                if spec.action.value == "skill:use" and capability.kind != "skill":
                    raise GovernanceConflict("Skill use grants must target a Skill capability")
            existing = session.scalar(select(AiResourceGrant).where(
                AiResourceGrant.identity_id == identity_id, AiResourceGrant.action == spec.action.value,
                AiResourceGrant.resource_key == spec.resource_key,
            ))
            if existing is not None:
                return public_record(existing)
            row = AiResourceGrant(id=uuid4().hex, project_key=project_key, identity_id=identity_id,
                                  **spec.model_dump(mode="json"), created_by=actor, create_time=utc_now())
            session.add(row)
            self._audit(session, actor, "resource_grant_created", project_key, identity_id=identity_id,
                        grant_id=row.id, action=spec.action.value, resource_key=spec.resource_key)
            session.flush()
            return public_record(row)

    def list_grants(self, project_key, identity_id, *, limit=100):
        with self._read() as session:
            if session.scalar(select(AiServiceIdentity.id).where(
                AiServiceIdentity.project_key == project_key, AiServiceIdentity.id == identity_id,
            )) is None:
                raise GovernanceNotFound("Service identity not found")
            return [public_record(row) for row in session.scalars(select(AiResourceGrant).where(
                AiResourceGrant.project_key == project_key, AiResourceGrant.identity_id == identity_id,
            ).order_by(AiResourceGrant.action, AiResourceGrant.resource_key).limit(limit))]

    def delete_grant(self, project_key, identity_id, grant_id, actor):
        with self._sf.begin() as session:
            self._project(session, project_key, lock=True)
            self._identity(session, project_key, identity_id)
            row = session.scalar(select(AiResourceGrant).where(
                AiResourceGrant.project_key == project_key, AiResourceGrant.identity_id == identity_id,
                AiResourceGrant.id == grant_id,
            ))
            if row is None:
                raise GovernanceNotFound("Resource grant not found")
            session.delete(row)
            self._audit(session, actor, "resource_grant_deleted", project_key, identity_id=identity_id, grant_id=grant_id)

    @staticmethod
    def _active_key(digest):
        return select(AiServiceAccessKey).join(AiServiceIdentity, AiServiceAccessKey.identity_id == AiServiceIdentity.id).join(
            AiProject, AiServiceIdentity.project_key == AiProject.key,
        ).where(AiServiceAccessKey.digest == digest, AiServiceAccessKey.revoked.is_(False),
                or_(AiServiceAccessKey.expires_at.is_(None), AiServiceAccessKey.expires_at > func.now()),
                AiServiceIdentity.enabled.is_(True), AiProject.enabled.is_(True))

    def authenticate(self, digest):
        with self._read() as session:
            row = session.scalar(self._active_key(digest))
            return public_record(row) if row is not None else None

    def execution_resources(self, project_key, identity_id, action):
        """Durable task authorization survives bearer rotation, not identity/grant revocation."""
        with self._read() as session:
            return set(session.scalars(select(AiResourceGrant.resource_key).join(
                AiServiceIdentity, AiResourceGrant.identity_id == AiServiceIdentity.id,
            ).join(AiProject, AiServiceIdentity.project_key == AiProject.key).where(
                AiResourceGrant.project_key == project_key, AiResourceGrant.identity_id == identity_id,
                AiResourceGrant.action == action, AiServiceIdentity.project_key == project_key,
                AiServiceIdentity.enabled.is_(True), AiProject.enabled.is_(True),
            )))

    def allowed_resources(self, digest, project_key, action):
        """One database snapshot rechecks key, project, identity, expiry AND grants."""
        active = self._active_key(digest).where(AiServiceAccessKey.project_key == project_key).subquery()
        with self._read() as session:
            return set(session.scalars(select(AiResourceGrant.resource_key).join(
                active, active.c.identity_id == AiResourceGrant.identity_id,
            ).where(AiResourceGrant.project_key == project_key, AiResourceGrant.action == action)))
