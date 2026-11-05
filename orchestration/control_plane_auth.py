"""Digest-backed administrator authentication with immediate database revocation."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from domain.governance import AccessDenied


@dataclass(frozen=True)
class ControlPlanePrincipal:
    name: str
    scopes: frozenset[str]
    project_key: str | None = None
    identity_id: str | None = None
    access_key_id: str | None = None
    credential_digest: str | None = field(default=None, repr=False)
    oidc_provider: str | None = None
    oidc_subject: str | None = field(default=None, repr=False)
    expires_at: float | None = None

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


class ControlPlaneAuth:
    def __init__(self, key_repository, governance_repository=None, oidc_repository=None):
        self.key_repository = key_repository
        self.governance = governance_repository
        self.oidc = oidc_repository

    def authenticate_bearer(self, token):
        binding = self.oidc.authenticate(token) if self.oidc else None
        if binding is None:
            return None
        return ControlPlanePrincipal(name=f"service:{binding['identity_id']}", scopes=frozenset(),
            project_key=binding["project_key"], identity_id=binding["identity_id"],
            oidc_provider=binding["provider_key"], oidc_subject=binding["subject"], expires_at=binding["expires_at"])

    def authenticate(self, token: str | None) -> ControlPlanePrincipal | None:
        if not token or len(token) > 4096 or token.startswith("sha256:"):
            return None
        try:
            digest = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        except UnicodeError:
            return None
        record = self.key_repository.find_by_digest(digest)
        if record is not None:
            if record.enabled:
                return ControlPlanePrincipal(record.name, frozenset(record.scopes))
            return None
        if self.governance is not None:
            service_key = self.governance.authenticate(digest)
            if service_key is not None:
                return ControlPlanePrincipal(
                    name=f"service:{service_key['identity_id']}", scopes=frozenset(),
                    project_key=service_key["project_key"], identity_id=service_key["identity_id"],
                    access_key_id=service_key["id"], credential_digest=digest,
                )
        return None

    def allowed_resources(self, principal, project_key, action, resource_key=None):
        if self.governance is None:
            raise AccessDenied("Governance is unavailable")
        if principal.has_scope("admin"):
            if not self.governance.get_project(project_key)["enabled"]:
                raise AccessDenied("Project is disabled")
            return {"*"}
        if principal.project_key != project_key or not principal.identity_id:
            raise AccessDenied("Resource access denied")
        if principal.oidc_provider:
            if self.oidc is None:
                raise AccessDenied("OIDC is unavailable")
            binding = self.oidc.active_binding(principal.oidc_provider, principal.oidc_subject)
            if (binding is None or binding["identity_id"] != principal.identity_id or
                    binding["project_key"] != project_key or principal.expires_at is None or time.time() >= principal.expires_at):
                raise AccessDenied("OIDC binding expired or revoked")
            resources = self.governance.execution_resources(project_key, principal.identity_id, action)
        else:
            resources = self.governance.allowed_resources(principal.credential_digest, project_key, action)
        if not resources or (resource_key is not None and "*" not in resources and resource_key not in resources):
            raise AccessDenied("Resource access denied")
        return resources
