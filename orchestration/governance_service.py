"""Generate service credentials only at issuance; never persist bearer tokens."""

from __future__ import annotations

import hashlib
import secrets

from domain import utc_now
from domain.governance import GovernanceError


class GovernanceService:
    def __init__(self, repository):
        self.repository = repository

    def issue_key(self, project_key, identity_id, spec, actor):
        if spec.expires_at is not None and spec.expires_at <= utc_now():
            raise GovernanceError("Access key expiry must be in the future")
        token = "eai_" + secrets.token_urlsafe(32)
        digest = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        record = self.repository.issue_key(project_key, identity_id, spec, digest, actor)
        return {**record, "token": token}
