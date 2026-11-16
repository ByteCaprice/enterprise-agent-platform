"""Independent convergence of expired or no-longer-authorized Tasks.

Recovery never retries an execution or assumes an external effect failed.
Multiple instances coordinate through database row locks.
"""

import asyncio
from uuid import uuid4

from interfaces.worker.lifecycle import observe_worker, stop_signals


async def run_recovery_worker(store, *, once=False, worker_id=None, poll_seconds=5):
    if not 1 <= poll_seconds <= 300:
        raise ValueError("Recovery interval must be between 1 and 300 seconds")
    stop = asyncio.Event()

    async def run():
        while not stop.is_set():
            rejected = await asyncio.to_thread(store.task_queue.reject_disabled_project_tasks)
            expired = await asyncio.to_thread(store.task_queue.recover_expired)
            if once:
                return {"rejected": rejected, "expired": expired}
            if not (rejected or expired):
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
        return None

    with stop_signals(stop):
        return await observe_worker(store, worker_id or f"recovery-{uuid4().hex}", "recovery", run)
