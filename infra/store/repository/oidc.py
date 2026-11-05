"""Installed issuer keys and explicit subject bindings; never auto-provisions identities."""

import time

import jwt
from sqlalchemy import select

from domain.task import TaskConflict, TaskError, TaskNotFound
from infra.store.model import AiProject, AiServiceIdentity
from infra.store.model.ai_operations import AiOIDCBinding, AiOIDCProvider
from infra.store.repository.base import BaseRepository
from infra.store.repository.operations import audit, record


class OIDCRepository(BaseRepository):
    def providers(self, spec=None, actor=None):
        with self._sf.begin() as session:
            if spec is not None:
                for key in spec.jwks["keys"]:
                    try:
                        parsed = jwt.PyJWK.from_dict(key, algorithm="RS256")
                    except (jwt.PyJWTError, ValueError, TypeError):
                        raise TaskError("Install a valid public RSA signing key") from None
                    if parsed.key.key_size < 2048:
                        raise TaskConflict("RSA signing keys must be at least 2048 bits")
                row = session.get(AiOIDCProvider, spec.key)
                duplicate = session.scalar(select(AiOIDCProvider).where(AiOIDCProvider.issuer == spec.issuer))
                if duplicate and duplicate.key != spec.key:
                    raise TaskConflict("Issuer is already installed")
                if row is None:
                    row = AiOIDCProvider(enabled=True, **spec.model_dump())
                    session.add(row)
                else:
                    if (row.issuer, row.audience) != (spec.issuer, spec.audience):
                        raise TaskConflict("Issuer/audience immutable; create a new provider")
                    row.jwks = spec.jwks  # explicit administrator key rotation
                audit(session, "platform", actor, "oidc_provider_keys_installed", spec.key)
                session.flush()
                return record(row)
            return [record(row) for row in session.scalars(select(AiOIDCProvider).order_by(AiOIDCProvider.key).limit(100))]

    def enabled(self, key, enabled, actor):
        with self._sf.begin() as session:
            row = session.get(AiOIDCProvider, key)
            if row is None:
                raise TaskNotFound("OIDC provider not found")
            row.enabled = enabled
            audit(session, "platform", actor, "oidc_provider_updated", key)
            session.flush()
            return record(row)

    def bindings(self, project, spec=None, actor=None):
        with self._sf.begin() as session:
            if spec:
                identity = session.get(AiServiceIdentity, spec.identity_id)
                if identity is None or identity.project_key != project or session.get(AiOIDCProvider, spec.provider_key) is None:
                    raise TaskNotFound("Provider or project identity not found")
                row = session.get(AiOIDCBinding, (spec.provider_key, spec.subject))
                if row and (row.project_key != project or row.identity_id != spec.identity_id):
                    raise TaskConflict("Subject already bound; remove binding explicitly before rebinding")
                if row is None:
                    row = AiOIDCBinding(project_key=project, enabled=True, **spec.model_dump())
                    session.add(row)
                audit(session, project, actor, "oidc_subject_bound", spec.provider_key)
                session.flush()
                return record(row)
            return [record(row) for row in session.scalars(select(AiOIDCBinding).where(
                AiOIDCBinding.project_key == project).order_by(AiOIDCBinding.provider_key, AiOIDCBinding.subject).limit(200))]

    def unbind(self, project, provider, subject, actor):
        with self._sf.begin() as session:
            row = session.get(AiOIDCBinding, (provider, subject))
            if row is None or row.project_key != project:
                raise TaskNotFound("OIDC binding not found")
            session.delete(row)
            audit(session, project, actor, "oidc_subject_unbound", provider)

    def active_binding(self, provider, subject):
        with self._read() as session:
            binding = session.scalar(select(AiOIDCBinding).join(AiOIDCProvider).join(
                AiServiceIdentity, AiServiceIdentity.id == AiOIDCBinding.identity_id).join(
                AiProject, AiProject.key == AiOIDCBinding.project_key).where(
                AiOIDCBinding.provider_key == provider, AiOIDCBinding.subject == subject,
                AiOIDCBinding.enabled.is_(True), AiOIDCProvider.enabled.is_(True),
                AiServiceIdentity.enabled.is_(True), AiProject.enabled.is_(True)))
            return record(binding) if binding else None

    def authenticate(self, token):
        try:
            if not token or len(token) > 16384:
                return None
            header = jwt.get_unverified_header(token)
            if (header.get("alg") != "RS256" or header.get("crit") or not isinstance(header.get("kid"), str) or
                    not 1 <= len(header["kid"]) <= 200):
                return None
            # Unverified issuer is only a lookup hint. No network/JWK URL from the token is used.
            hint = jwt.decode(token, options={"verify_signature": False})
            if not isinstance(hint.get("iss"), str) or not 1 <= len(hint["iss"]) <= 2048 or "\x00" in hint["iss"]:
                return None
            with self._read() as session:
                provider = session.scalar(select(AiOIDCProvider).where(AiOIDCProvider.issuer == hint["iss"],
                                                                       AiOIDCProvider.enabled.is_(True)))
                if provider is None:
                    return None
                key = next((key for key in provider.jwks["keys"] if key["kid"] == header["kid"]), None)
                if key is None:
                    return None
                claims = jwt.decode(token, jwt.PyJWK.from_dict(key, algorithm="RS256"), algorithms=["RS256"],
                    issuer=provider.issuer, audience=provider.audience,
                    options={"require": ["exp", "iat", "sub", "iss", "aud"], "strict_aud": True})
                if (not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 300 or "\x00" in claims["sub"] or
                        type(claims["exp"]) not in {int, float} or type(claims["iat"]) not in {int, float} or
                        not claims["iat"] <= time.time() < claims["exp"]):
                    return None
                provider_key = provider.key
            binding = self.active_binding(provider_key, claims["sub"])
            return (binding | {"expires_at": claims["exp"]}) if binding else None
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None
