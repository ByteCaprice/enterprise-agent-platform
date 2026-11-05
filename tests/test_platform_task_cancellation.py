"""Small cancellation-boundary smokes: owned schemas, synthetic turns, no Node/provider IO."""

import asyncio
from dataclasses import replace

from sqlalchemy import text
from test_platform_agent_broker import MockTurns, make_broker
from test_platform_agent_storage import root_fixture
from test_platform_tasks import ADMIN, service_identity, submit
from test_platform_tasks import inbox as inbox_fixture

from domain.task_control import ControlState
from domain.task_execution import CompletionOutcome
from orchestration.platform_worker import PlatformWorker, WorkerPolicy

inbox = inbox_fixture


def test_queued_receipt_is_idempotent_and_terminal_completion_wins(inbox):
    client, store = inbox
    target = submit(client).headers["Location"]
    assert client.get(target + "/cancellation", headers=ADMIN).json()["cancellation"] is None
    cancelled = client.post(target + "/cancel", headers=ADMIN)
    assert cancelled.status_code == 200
    receipt = cancelled.json()
    assert receipt["task"]["status"] == "cancelled"
    assert receipt["cancellation"]["state"] == "acknowledged"
    assert client.post(target + "/cancel", headers=ADMIN).json() == receipt
    assert client.get(target + "/attempts", headers=ADMIN).json() == []
    target = submit(client, key="success-first").headers["Location"]
    claim = store.tasks.claim("completion-first")
    assert store.tasks.complete(claim.lease, status="succeeded", result={"ok": True}) == CompletionOutcome.COMPLETED
    assert client.post(target + "/cancel", headers=ADMIN).status_code == 409


def test_running_intent_defers_success_and_expired_owner_cannot_acknowledge(inbox):
    client, store = inbox
    target = submit(client).headers["Location"]
    claim = store.tasks.claim("cancel-fence")
    receipt = client.post(target + "/cancel", headers=ADMIN)
    assert receipt.status_code == 202 and receipt.json()["task"]["status"] == "running"
    assert receipt.json()["cancellation"]["state"] == "requested"
    assert store.tasks.complete(claim.lease, status="succeeded", result={}) == CompletionOutcome.CANCELLATION_PENDING
    assert not store.task_control.acknowledge_cancel(replace(claim.lease, token="0" * 64))
    observation = store.task_control.observe(claim.lease, lease_seconds=30, cancellation_grace_seconds=5)
    assert observation.state == ControlState.CANCELLATION_REQUESTED
    assert client.get(target + "/cancellation", headers=ADMIN).json()["cancellation"]["state"] == "observed"
    with store.engine.begin() as connection:
        connection.execute(text("UPDATE ai_task_attempt SET lease_expires_at=start_time WHERE id=:id"),
                           {"id": claim.lease.attempt_id})
    assert not store.task_control.acknowledge_cancel(claim.lease)
    assert store.tasks.recover_expired() == 1
    receipt = client.get(target + "/cancellation", headers=ADMIN).json()
    assert receipt["task"]["status"] == "failed" and receipt["cancellation"]["state"] == "unconfirmed"
    assert receipt["cancellation"]["acknowledged_at"] is None
    assert client.get(target + "/result", headers=ADMIN).json()["error_code"] == "CANCEL_LEASE_EXPIRED_RESULT_UNKNOWN"


def test_worker_discards_cancelled_model_decision_but_keeps_usage_and_real_tree(inbox, monkeypatch):
    client, store = inbox
    _, _, headers = service_identity(store, actions=("task:submit", "task:read", "task:cancel", "model:invoke", "prompt:render", "tool:invoke"))
    _, _, target = root_fixture(client, store, monkeypatch, begin=False, headers=headers, claim_task=False)

    async def scenario():
        started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class ControlledTurns(MockTurns):
            async def generate_turn(self, request):
                started.set()
                await release.wait()
                return await super().generate_turn(request)

        transport = ControlledTurns()

        async def execute(claim):
            try:
                broker = make_broker(claim, store, transport)
                await broker.open()
                await broker.request_model()
                raise AssertionError("A cancelled model decision must never reach the runtime")
            finally:
                stopped.set()

        policy = WorkerPolicy(lease_seconds=30, heartbeat_seconds=5, execution_timeout_seconds=30)
        worker = PlatformWorker(store, "cancel-worker", {"agent": execute}, policy=policy)
        running = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(started.wait(), 15)
            response = await asyncio.to_thread(client.post, target + "/cancel", headers=headers)
            assert response.status_code == 202
            release.set()  # response arrives after intent, before the next monitor tick
            assert await asyncio.wait_for(running, 15)
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        assert stopped.is_set() and len(transport.requests) == 1

    asyncio.run(scenario())
    receipt = client.get(target + "/cancellation", headers=headers).json()
    assert receipt["task"]["status"] == "cancelled" and receipt["cancellation"]["state"] == "acknowledged"
    attempts = client.get(target + "/attempts", headers=headers).json()
    assert attempts[0]["cancel_acknowledged_at"] is not None and attempts[0]["status"] == "cancelled"
    calls = client.get(target + "/model-calls", headers=headers).json()
    assert calls[0]["total_tokens"] == 7 and calls[0]["output_hash"] is None
    assert client.get(target + "/tool-calls", headers=headers).json() == []
    executions = client.get(target + "/agent-executions", headers=headers).json()["items"]
    steps = client.get(target + f"/agent-executions/{executions[0]['id']}/steps", headers=headers).json()["items"]
    assert len(steps) == 1 and steps[0]["kind"] == "model_turn" and "dispatch_id" not in steps[0]
    tree_url = target + f"/attempts/{attempts[0]['id']}/execution-tree"
    items, cursor = [], None
    while True:
        response = client.get(tree_url, headers=headers, params={"limit": 1, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200
        page = response.json()
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [item["kind"] for item in items] == ["agent_execution", "agent_step", "model_call"]
    assert items[-1]["parent"] == {"kind": "agent_step", "id": steps[0]["id"]}
