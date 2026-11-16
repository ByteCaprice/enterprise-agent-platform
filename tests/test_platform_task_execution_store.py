"""Real-PostgreSQL tests for task execution persistence and lease fencing."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_platform_tasks import inbox as inbox_fixture
from test_platform_tasks import publish_completion_flow, record_completion_node, submit
from worker_support import worker_capabilities

from domain.task_execution import CompletionOutcome
from infra.store.model import AiTask, AiTaskAttempt

inbox = inbox_fixture


def enqueue(client, key="execution-key", *, store=None):
    changes = {}
    if store is not None:
        if not any(item["key"] == "completion-flow" for item in store.capabilities.list("payments")):
            publish_completion_flow(store)
        changes["capability_key"] = "completion-flow"
    response = submit(client, key=key, input={"name": "private-input"}, **changes)
    assert response.status_code == 202
    return response.json()


def test_concurrent_workers_claim_distinct_tasks_once(inbox):
    client, store = inbox
    enqueue(client, "one")
    enqueue(client, "two")
    with ThreadPoolExecutor(max_workers=2) as workers:
        claims = list(workers.map(lambda worker: store.task_queue.claim(worker, capabilities=worker_capabilities()), ["worker-a", "worker-b"]))
    assert all(claim is not None for claim in claims)
    assert len({claim.task.id for claim in claims}) == 2
    assert len({claim.lease.token for claim in claims}) == 2
    assert {claim.task.status for claim in claims} == {"running"}


def test_heartbeat_fencing_expiry_and_no_resurrection(inbox):
    client, store = inbox
    task = enqueue(client)
    claim = store.task_queue.claim("worker-a", lease_seconds=1, capabilities=worker_capabilities())
    lease = claim.lease
    assert store.task_queue.heartbeat(lease, lease_seconds=1)
    assert not store.task_queue.heartbeat(replace(lease, worker_id="worker-b"))
    time.sleep(1.1)
    assert not store.task_queue.heartbeat(lease)
    assert store.task_completion.complete(lease, status="failed", error_code="SAFE_ERROR") == CompletionOutcome.LEASE_LOST
    assert store.task_queue.recover_expired() == 1
    assert store.task_queue.claim("worker-b", capabilities=worker_capabilities()) is None
    result = store.tasks.result("payments", task["id"], allowed_keys=["*"], owner=None)
    assert result.status == "failed" and result.error_code == "LEASE_EXPIRED_RESULT_UNKNOWN"


def test_complete_rejects_stale_owner_token_and_conflicting_results(inbox):
    client, store = inbox
    task = enqueue(client, store=store)
    claim = store.task_queue.claim("worker-a", capabilities=worker_capabilities())
    lease = claim.lease
    assert store.task_completion.complete(replace(lease, token="0" * 64), status="failed", error_code="SAFE_ERROR") == CompletionOutcome.LEASE_LOST
    assert store.task_completion.complete(replace(lease, worker_id="worker-b"), status="failed", error_code="SAFE_ERROR") == CompletionOutcome.LEASE_LOST
    with pytest.raises(ValueError):
        store.task_completion.complete(lease, status="succeeded", result={"ok": True}, error_code="SAFE_ERROR")
    with pytest.raises(ValueError):
        store.task_completion.complete(lease, status="succeeded", result={"not-json": float("nan")})
    record_completion_node(store, claim, {"ok": True})
    assert store.task_completion.complete(lease, status="succeeded", result={"ok": True}) == CompletionOutcome.COMPLETED
    assert store.task_completion.complete(lease, status="failed", error_code="SAFE_ERROR") == CompletionOutcome.LEASE_LOST
    result = store.tasks.result("payments", task["id"], allowed_keys=["*"], owner=None)
    assert result.result == {"ok": True}


def test_output_schema_validation_and_safe_error_codes(inbox):
    client, store = inbox
    enqueue(client)
    claim = store.task_queue.claim("worker-a", capabilities=worker_capabilities())
    # The pinned published schema is an object; enforce local schema references.
    with pytest.raises(ValueError):
        store.task_completion.complete(claim.lease, status="succeeded", result={"bad": float("inf")})
    with pytest.raises(ValueError):
        store.task_completion.complete(claim.lease, status="failed", error_code="private failure detail")
    assert store.task_completion.complete(claim.lease, status="failed", error_code="SAFE_FAILURE") == CompletionOutcome.COMPLETED


def test_attempt_result_scoping_and_metadata_excludes_secrets(inbox):
    client, store = inbox
    task = enqueue(client, store=store)
    claim = store.task_queue.claim("private-worker", capabilities=worker_capabilities())
    record_completion_node(store, claim, {"safe": True})
    assert store.task_completion.complete(claim.lease, status="succeeded", result={"safe": True}) == CompletionOutcome.COMPLETED
    attempts = store.tasks.attempts("payments", task["id"], allowed_keys=["*"], owner=None)
    assert len(attempts) == 1 and attempts[0].worker_id == "private-worker"
    serialized = attempts[0].model_dump_json()
    assert claim.lease.token not in serialized and "private-input" not in serialized
    from domain.task import TaskNotFound

    with pytest.raises(TaskNotFound):
        store.tasks.attempts("other", task["id"], allowed_keys=["*"], owner=None)
    with pytest.raises(TaskNotFound):
        store.tasks.result("payments", task["id"], allowed_keys=[], owner=None)
    with Session(store.engine) as session:
        row = session.get(AiTask, task["id"])
        assert row is not None and row.input == {"name": "private-input"}
        assert row.result == {"safe": True}
        attempt = session.scalar(select(AiTaskAttempt).where(AiTaskAttempt.task_id == task["id"]))
        assert attempt is not None and attempt.token == claim.lease.token


def test_claim_audit_failure_rolls_back_task_and_attempt(inbox, monkeypatch):
    client, store = inbox
    task = enqueue(client)

    def fail(*_args, **_kwargs):
        raise RuntimeError("audit failure")

    monkeypatch.setattr("infra.store.repository.task_queue.record_task_transition", fail)
    with pytest.raises(RuntimeError):
        store.task_queue.claim("worker-a", capabilities=worker_capabilities())
    with Session(store.engine) as session:
        row = session.get(AiTask, task["id"])
        assert row is not None and row.status == "queued"
        assert session.scalar(select(AiTaskAttempt.id).where(AiTaskAttempt.task_id == task["id"])) is None
