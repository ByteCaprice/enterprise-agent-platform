"""Process observation and bounded fail-closed shutdown, never automatic Task replay."""

import asyncio
import contextlib
import logging
import signal

logger = logging.getLogger("enterprise_ai.worker")


@contextlib.contextmanager
def stop_signals(stop):
    loop = asyncio.get_running_loop()
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                old = signal.getsignal(signum)
                loop.add_signal_handler(signum, stop.set)
                previous[signum] = old
            except (NotImplementedError, RuntimeError, ValueError):
                continue
        yield
    finally:
        for signum, old in previous.items():
            loop.remove_signal_handler(signum)
            signal.signal(signum, old)


async def observe_worker(store, worker_id, role, run, *, executors=(), workflow_agents=False, capabilities=None):
    instance = await asyncio.to_thread(store.worker_health.start, worker_id, role, executors, workflow_agents,
                                       capabilities=capabilities)

    async def heartbeat():
        while True:
            await asyncio.sleep(5)
            await asyncio.wait_for(asyncio.to_thread(store.worker_health.heartbeat, instance), timeout=10)

    runner = asyncio.create_task(run())
    observer = asyncio.create_task(heartbeat())
    state = "failed"
    try:
        done, _ = await asyncio.wait({runner, observer}, return_when=asyncio.FIRST_COMPLETED)
        if observer in done:
            observer.result()
            raise RuntimeError("Worker observer stopped unexpectedly")
        result = runner.result()
        state = "stopped"
        return result
    finally:
        # DB failure stops this process rather than repeating an uncertain claim,
        # delivery or execution. A supervisor may restart a fresh process.
        runner.cancel()
        observer.cancel()
        await asyncio.gather(runner, observer, return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.to_thread(store.worker_health.heartbeat, instance, state), timeout=10)
        logger.info("worker_exit instance=%s state=%s", instance, state)
