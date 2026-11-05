"""Independent recovery never replays an expired execution."""

import asyncio
import time

from test_platform_tasks import inbox as inbox_fixture
from test_platform_tasks import submit

from interfaces.worker.recovery import run_recovery_worker

inbox = inbox_fixture


def test_recovery_worker_converges_without_executor(inbox):
    client, store = inbox
    task = submit(client).json()
    claim = store.tasks.claim("crashed-worker", lease_seconds=1)
    time.sleep(1.1)
    result = asyncio.run(run_recovery_worker(store, once=True))
    assert result == {"rejected": 0, "expired": 1}
    record = store.tasks.result("payments", task["id"], allowed_keys={"*"}, owner=None)
    assert record.status == "failed" and record.error_code == "LEASE_EXPIRED_RESULT_UNKNOWN"
    assert store.tasks.claim("another-worker") is None
    assert not store.tasks.heartbeat(claim.lease)
