"""Independent cancellation regressions for the new-platform worker."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import text
from test_platform_tasks import inbox as inbox_fixture
from test_platform_tasks import submit

from domain.task_control import ControlState, ExecutionObservation
from domain.task_execution import CompletionOutcome
from orchestration.platform_worker import PlatformWorker, WorkerPolicy

inbox = inbox_fixture


def test_repeated_worker_cancellation_never_releases_worker_while_adapter_runs(inbox):
    client, store = inbox
    submit(client)
    policy = WorkerPolicy(lease_seconds=10, heartbeat_seconds=0.1, execution_timeout_seconds=5,
                          cancellation_grace_seconds=1)

    async def scenario():
        started = asyncio.Event()
        cancelling = asyncio.Event()
        release = asyncio.Event()
        stopped = asyncio.Event()

        async def execute(_claim):
            started.set()
            try:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelling.set()
            finally:
                stopped.set()
            return {"done": True}

        worker = PlatformWorker(store, "cancellation-test", {"agent": execute}, policy=policy)
        running = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(started.wait(), 5)
            running.cancel()
            await asyncio.wait_for(cancelling.wait(), 5)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert not stopped.is_set()
            with pytest.raises(RuntimeError, match="unavailable|already executing"):
                await worker.run_once()
        finally:
            release.set()
            await asyncio.wait_for(stopped.wait(), 5)

    asyncio.run(scenario())


def test_claim_skips_locked_oldest_task(inbox):
    client, store = inbox
    first = submit(client, key="locked-first").json()["id"]
    second = submit(client, key="unlocked-second").json()["id"]
    with store.engine.begin() as connection:
        connection.execute(text("SELECT id FROM ai_task WHERE id = :id FOR UPDATE"), {"id": first})
        with ThreadPoolExecutor(max_workers=1) as pool:
            claim = pool.submit(store.tasks.claim, "other-worker").result(timeout=5)
        assert claim is not None and claim.task.id == second
    assert store.tasks.claim("next-worker").task.id == first


def test_each_lease_identity_component_fences_heartbeat_and_completion(inbox):
    client, store = inbox
    first = submit(client, key="first-fence").json()["id"]
    second = submit(client, key="second-fence").json()["id"]
    one, two = store.tasks.claim("worker"), store.tasks.claim("worker")
    assert {one.task.id, two.task.id} == {first, second}
    for forged in (replace(one.lease, task_id=two.lease.task_id),
                   replace(one.lease, attempt_id=two.lease.attempt_id),
                   replace(one.lease, worker_id="different"),
                   replace(one.lease, token=two.lease.token)):
        assert not store.tasks.heartbeat(forged)
        assert store.tasks.complete(forged, status="failed", error_code="SAFE_ERROR") == CompletionOutcome.LEASE_LOST
    assert store.tasks.complete(one.lease, status="succeeded", result={"ok": True}) == CompletionOutcome.COMPLETED
    assert store.tasks.complete(two.lease, status="succeeded", result={"ok": True}) == CompletionOutcome.COMPLETED


def test_heartbeat_exception_cancels_adapter_without_terminal_write(inbox, monkeypatch):
    client, store = inbox
    task_id = submit(client).json()["id"]
    stopped = []

    async def execute(_claim):
        try:
            await asyncio.sleep(60)
        finally:
            stopped.append(True)
        return {}

    original_heartbeat = store.task_control.observe
    calls = 0

    def fail_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("sensitive database detail")
        return original_heartbeat(*args, **kwargs)

    monkeypatch.setattr(store.task_control, "observe", fail_on_second)
    policy = WorkerPolicy(lease_seconds=10, heartbeat_seconds=0.1, execution_timeout_seconds=5,
                          cancellation_grace_seconds=1)
    assert asyncio.run(PlatformWorker(store, "worker", {"agent": execute}, policy=policy).run_once())
    assert stopped == [True] and calls == 2
    result = store.tasks.result("payments", task_id, allowed_keys={"*"}, owner=None)
    assert result.status == "running" and result.result is None and result.error_code is None


def test_expired_lease_before_dispatch_never_invokes_adapter(inbox, monkeypatch):
    client, store = inbox
    task_id = submit(client).json()["id"]
    original_claim = store.tasks.claim

    def expire_after_claim(*args, **kwargs):
        claim = original_claim(*args, **kwargs)
        with store.engine.begin() as connection:
            connection.execute(text("UPDATE ai_task_attempt SET lease_expires_at = start_time WHERE id = :id"),
                               {"id": claim.lease.attempt_id})
        return claim

    monkeypatch.setattr(store.tasks, "claim", expire_after_claim)
    called = []

    async def execute(_claim):
        called.append(True)
        return {}

    assert asyncio.run(PlatformWorker(store, "worker", {"agent": execute}).run_once())
    assert not called
    assert store.tasks.recover_expired() == 1
    result = store.tasks.result("payments", task_id, allowed_keys={"*"}, owner=None)
    assert result.status == "failed" and result.error_code == "LEASE_EXPIRED_RESULT_UNKNOWN"


@pytest.mark.parametrize("_iteration", range(3))
def test_hanging_heartbeat_stops_execution_within_lease(inbox, monkeypatch, _iteration):
    client, store = inbox
    task_id = submit(client).json()["id"]
    release = threading.Event()
    calls = 0
    times = {}

    def heartbeat(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            times["renewal_started"] = time.monotonic()
            return ExecutionObservation(ControlState.ACTIVE)
        times["blocked"] = time.monotonic()
        release.wait(timeout=10)
        return ExecutionObservation(ControlState.LEASE_LOST)

    async def execute(_claim):
        times["started"] = time.monotonic()
        try:
            await asyncio.sleep(60)
        finally:
            times["stopped"] = time.monotonic()
        return {}

    monkeypatch.setattr(store.task_control, "observe", heartbeat)
    policy = WorkerPolicy(lease_seconds=3, heartbeat_seconds=0.05, execution_timeout_seconds=10,
                          cancellation_grace_seconds=0.2)

    worker = PlatformWorker(store, "worker", {"agent": execute}, policy=policy)
    # Isolate monitor timing from SIT latency; the preceding test exercises the real pre-dispatch fence.
    monkeypatch.setattr(worker.authorizer, "allowed", lambda _claim: True)

    async def scenario():
        try:
            assert await asyncio.wait_for(worker.run_once(), 8)
        finally:
            release.set()

    asyncio.run(scenario())
    assert calls == 2
    assert times["renewal_started"] <= times["started"] <= times["blocked"] <= times["stopped"]
    assert times["stopped"] - times["renewal_started"] < policy.lease_seconds
    result = store.tasks.result("payments", task_id, allowed_keys={"*"}, owner=None)
    assert result.status == "running" and result.result is None and result.error_code is None
