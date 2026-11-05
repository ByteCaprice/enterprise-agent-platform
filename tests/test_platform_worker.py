"""Real SIT-isolated Task -> Attempt -> installed adapter -> scoped result tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import text
from test_platform_tasks import ADMIN, service_identity, submit
from test_platform_tasks import inbox as inbox_fixture

from domain.task_control import ControlState, ExecutionObservation
from orchestration.platform_worker import PlatformWorker, WorkerPolicy

inbox = inbox_fixture

POLICY = WorkerPolicy(lease_seconds=10, heartbeat_seconds=0.1, execution_timeout_seconds=5,
                      cancellation_grace_seconds=0.5)


def run(store, executor, policy=POLICY):
    return asyncio.run(PlatformWorker(store, "new-worker", {"agent": executor}, policy=policy).run_once())


def test_installed_adapter_executes_pinned_task_and_result_is_separate_from_metadata(inbox):
    client, store = inbox
    accepted = submit(client)
    target = accepted.headers["Location"]
    assert client.get(target + "/attempts", headers=ADMIN).json() == []
    assert client.get(target + "/result", headers=ADMIN).json() == {
        "task_id": accepted.json()["id"], "status": "queued", "result": None, "error_code": None,
    }
    calls = []

    async def execute(claim):
        calls.append(claim)
        assert claim.artifact.version == claim.task.version == "1"
        return {"synthetic_output": claim.input["name"]}

    assert run(store, execute)
    assert len(calls) == 1
    record = client.get(target, headers=ADMIN).json()
    assert record["status"] == "succeeded" and "result" not in record and "input" not in record
    result = client.get(target + "/result", headers=ADMIN)
    assert result.headers["Cache-Control"] == "no-store"
    assert result.json()["result"] == {"synthetic_output": "Synthetic customer"}
    attempts = client.get(target + "/attempts", headers=ADMIN).json()
    assert len(attempts) == 1 and attempts[0]["status"] == "succeeded"
    assert not {"input", "token", "result", "definition"} & attempts[0].keys()
    assert calls[0].lease.token not in repr(calls[0])
    assert not run(store, execute)
    assert submit(client).json()["status"] == "succeeded"


def test_gateway_bounded_sync_wait_reuses_durable_task(inbox):
    client, store = inbox
    task = {"capability_key": "review", "version": "1", "input": {"name": "Synthetic"}}
    headers = {**ADMIN, "Idempotency-Key": "sync-request"}
    response = client.post("/v2/projects/payments/tasks/execute", headers=headers,
                           json={"task": task, "mode": "sync", "wait_seconds": 0.01})
    assert response.status_code == 202 and response.json()["wait_timed_out"] is True
    task_id = response.json()["task"]["id"]

    async def execute(_claim):
        return {"done": True}

    assert run(store, execute)
    again = client.post("/v2/projects/payments/tasks/execute", headers=headers,
                        json={"task": task, "mode": "sync", "wait_seconds": 5})
    assert again.status_code == 200 and again.json()["task"]["id"] == task_id
    assert again.json()["result"]["result"] == {"done": True}


def test_result_and_attempts_recheck_identity_and_capability_scope(inbox):
    client, store = inbox
    identity, _, headers = service_identity(store)
    _, _, other_headers = service_identity(store, key="other-business")
    target = submit(client, headers=headers).headers["Location"]

    async def execute(_claim):
        return {"private": "synthetic"}

    assert run(store, execute)
    for suffix in ("/attempts", "/result"):
        assert client.get(target + suffix, headers=other_headers).status_code == 404
        assert client.get(target + suffix, headers=headers).status_code == 200
        assert client.get(target + suffix).status_code == 401
    for grant in store.governance.list_grants("payments", identity["id"]):
        if grant["action"] == "task:read":
            store.governance.delete_grant("payments", identity["id"], grant["id"], "task-admin")
    assert client.get(target + "/result", headers=headers).status_code == 403
    assert client.get(target + "/attempts", headers=headers).status_code == 403


@pytest.mark.parametrize("revoke", ["identity", "grant", "project", "admin"])
def test_permission_shrink_before_execution_never_calls_adapter(inbox, revoke):
    client, store = inbox
    identity, _, headers = service_identity(store)
    target = submit(client, headers=ADMIN if revoke == "admin" else headers).headers["Location"]
    if revoke == "identity":
        store.governance.set_identity_enabled("payments", identity["id"], False, "task-admin")
    elif revoke == "project":
        store.governance.set_project_enabled("payments", False, "task-admin")
    elif revoke == "admin":
        record = store.api_key_configs.list()[0]
        store.api_key_configs.save(record.model_copy(update={"enabled": False}))
    else:
        for grant in store.governance.list_grants("payments", identity["id"]):
            if grant["action"] == "task:submit":
                store.governance.delete_grant("payments", identity["id"], grant["id"], "task-admin")

    async def execute(_claim):
        pytest.fail("Unauthorized task must not execute")

    assert run(store, execute)
    task_id = target.rsplit("/", 1)[1]
    result = store.tasks.result("payments", task_id, allowed_keys={"*"}, owner=None)
    assert result.status == "failed" and result.error_code == "EXECUTION_NOT_AUTHORIZED"


def test_rotated_bearer_does_not_invalidate_already_accepted_identity_work(inbox):
    client, store = inbox
    identity, key, headers = service_identity(store)
    accepted = submit(client, headers=headers)
    store.governance.revoke_key("payments", identity["id"], key["id"], "task-admin")

    async def execute(_claim):
        return {"done": True}

    assert run(store, execute)
    assert client.get(accepted.headers["Location"], headers=ADMIN).json()["status"] == "succeeded"


@pytest.mark.parametrize("bad_output", [True, False])
def test_adapter_failure_or_invalid_output_is_sanitized_without_retry(inbox, bad_output):
    client, store = inbox
    target = submit(client).headers["Location"]

    async def execute(_claim):
        if bad_output:
            return {"not_finite": float("nan")}
        raise RuntimeError("synthetic-secret-error")

    assert run(store, execute)
    result = client.get(target + "/result", headers=ADMIN)
    assert result.json()["status"] == "failed"
    assert result.json()["error_code"] == "EXECUTION_FAILED_RESULT_UNKNOWN"
    assert "synthetic-secret" not in result.text and result.json()["result"] is None
    assert not run(store, execute)


def test_timeout_cancels_adapter_without_automatic_reexecution(inbox):
    client, store = inbox
    target = submit(client).headers["Location"]
    cancelled = []

    async def execute(_claim):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    assert run(store, execute, replace(POLICY, execution_timeout_seconds=0.02))
    assert cancelled == [True]
    result = client.get(target + "/result", headers=ADMIN).json()
    assert result["status"] == "timed_out" and result["error_code"] == "EXECUTION_TIMEOUT_RESULT_UNKNOWN"
    assert not run(store, execute)


def test_lost_lease_cancels_execution_and_recovery_marks_unknown(inbox, monkeypatch):
    client, store = inbox
    target = submit(client).headers["Location"]
    stopped = []

    async def execute(_claim):
        try:
            await asyncio.sleep(60)
        finally:
            stopped.append(True)

    real_heartbeat = store.task_control.observe
    renewals = []

    def lose_after_dispatch(*args, **kwargs):
        renewals.append(True)
        return real_heartbeat(*args, **kwargs) if len(renewals) == 1 else ExecutionObservation(ControlState.LEASE_LOST)

    monkeypatch.setattr(store.task_control, "observe", lose_after_dispatch)
    assert run(store, execute)
    assert stopped == [True]
    assert client.get(target, headers=ADMIN).json()["status"] == "running"
    # Simulate time passage only in this test-owned schema, never persistent SIT.
    with store.engine.begin() as connection:
        connection.execute(text("UPDATE ai_task_attempt SET lease_expires_at = start_time WHERE status = 'running'"))
    assert store.tasks.recover_expired() == 1
    result = client.get(target + "/result", headers=ADMIN).json()
    assert result["error_code"] == "LEASE_EXPIRED_RESULT_UNKNOWN"
    assert not run(store, execute)


def test_concurrent_run_once_on_same_worker_is_rejected(inbox):
    client, store = inbox
    submit(client)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def execute(_claim):
            started.set()
            await release.wait()
            return {"done": True}

        worker = PlatformWorker(store, "single-owner", {"agent": execute}, policy=POLICY)
        task = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(RuntimeError, match="already executing"):
            await worker.run_once()
        release.set()
        assert await task

    asyncio.run(scenario())
