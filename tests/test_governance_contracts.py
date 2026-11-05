"""Pure identity contract/security boundaries, without any network or database."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from domain import utc_now
from domain.governance import AccessKeyCreate, EnabledUpdate, GovernanceError, ProjectCreate, ResourceGrantCreate
from orchestration.control_plane_auth import ControlPlaneAuth
from orchestration.governance_service import GovernanceService


@pytest.mark.parametrize("payload", [
    {"key": "valid", "name": "Nul\x00"},
    {"key": "valid", "name": "Surrogate\ud800"},
    {"key": "invalid/project", "name": "Project"},
    {"key": "valid", "name": "Project", "scopes": ["admin"]},
])
def test_project_contract_rejects_unsafe_text_and_injected_authority(payload):
    with pytest.raises(ValidationError):
        ProjectCreate.model_validate(payload)


@pytest.mark.parametrize("action", ["admin", "*", "tool:write", "model:write"])
def test_unimplemented_permissions_cannot_be_granted(action):
    with pytest.raises(ValidationError):
        ResourceGrantCreate(action=action, resource_key="*")


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_enabled_state_requires_a_real_boolean(value):
    with pytest.raises(ValidationError):
        EnabledUpdate(enabled=value)


def test_expired_key_is_rejected_before_any_repository_write():
    service = GovernanceService(SimpleNamespace())
    with pytest.raises(GovernanceError, match="future"):
        service.issue_key("payments", "identity", AccessKeyCreate(
            name="test", expires_at=utc_now() - timedelta(seconds=1)), "admin")


def test_key_generation_passes_only_a_digest_to_persistence():
    digests = []

    def persist(_project, _identity, _spec, digest, _actor):
        digests.append(digest)
        return {"id": str(len(digests))}

    service = GovernanceService(SimpleNamespace(issue_key=persist))
    issued = [service.issue_key("payments", "identity", AccessKeyCreate(name="test"), "admin") for _ in range(2)]
    assert issued[0]["token"] != issued[1]["token"]
    for item, digest in zip(issued, digests, strict=True):
        assert item["token"].startswith("eai_") and len(item["token"]) >= 40
        assert digest == "sha256:" + hashlib.sha256(item["token"].encode()).hexdigest()
        assert item["token"] not in digest


@pytest.mark.parametrize("token", [None, "", "sha256:" + "a" * 64, "x" * 4097, "\ud800"])
def test_malformed_bearer_keys_fail_before_repository_lookup(token):
    assert ControlPlaneAuth(SimpleNamespace()).authenticate(token) is None
