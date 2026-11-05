from __future__ import annotations

import hashlib

import pytest
from conftest import make_store

from domain import ApiKeyConfig
from orchestration.control_plane_auth import ControlPlaneAuth

pytestmark = pytest.mark.usefixtures("pg_url")


def test_digest_authentication_and_revocation_are_checked_for_each_request():
    store = make_store()
    token = "synthetic-secret-token"
    digest = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
    record = ApiKeyConfig(name="admin", api_key=digest, scopes=["admin"])
    store.api_key_configs.save(record)
    auth = ControlPlaneAuth(store.api_key_configs)

    assert auth.authenticate(token).has_scope("admin")
    assert not auth.authenticate(token).has_scope("runs")
    assert auth.authenticate(digest) is None
    assert auth.authenticate("wrong") is None
    assert auth.authenticate(None) is None

    record.enabled = False
    store.api_key_configs.save(record)
    assert auth.authenticate(token) is None


def test_deleting_last_key_never_enables_anonymous_access():
    store = make_store()
    auth = ControlPlaneAuth(store.api_key_configs)
    assert auth.authenticate(None) is None
    assert auth.authenticate("anything") is None
