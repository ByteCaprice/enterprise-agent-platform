"""Two local ownership checks, without database, runtime SDK or network."""

import asyncio
from types import SimpleNamespace

import pytest
from test_execution_context import claim_for
from worker_support import worker_capabilities

from domain.task_control import ControlState, ExecutionObservation
from domain.task_worker import WorkerServices
from infra.schema_validation import IsolatedJSONValidator
from orchestration.platform_worker import PlatformWorker, WorkerExecutionUnstoppable, WorkerPolicy


@pytest.mark.parametrize("unstoppable", [False, True])
def test_monitor_acknowledges_only_after_local_cleanup_and_poison_never_acknowledges(unstoppable):
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()
        intent = False
        acknowledgements, abandonments = [], []
        claim = claim_for()

        class Control:
            def observe(self, *_args, **_kwargs):
                return ExecutionObservation(ControlState.CANCELLATION_REQUESTED if intent else ControlState.ACTIVE)

            def acknowledge_cancel(self, _lease):
                assert stopped.is_set() and not unstoppable
                acknowledgements.append(True)
                return True

            def abandon_cancel(self, _lease):
                abandonments.append(True)
                return True

        def no_completion(*_args, **_kwargs):
            pytest.fail("Cancellation must use its own stop acknowledgement, not ordinary completion")

        services = WorkerServices(
            control=Control(), queue=SimpleNamespace(
                recover_expired=lambda: 0, reject_disabled_project_tasks=lambda: 0,
                claim=lambda *_args, **_kwargs: claim,
            ), completion=SimpleNamespace(complete=no_completion),
            authorization=SimpleNamespace(allowed=lambda _: True), validation=IsolatedJSONValidator(),
        )

        class Worker(PlatformWorker):
            async def _execute(self, _claim):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if unstoppable:
                        raise WorkerExecutionUnstoppable("Synthetic unreaped resource") from None
                    stopped.set()
                    raise

        async def unused(_claim):
            return {}

        worker = Worker(services, "control-check", {"workflow": unused}, policy=WorkerPolicy(
            heartbeat_seconds=0.01, cancellation_grace_seconds=0.2,
        ), capabilities=worker_capabilities("workflow"))
        running = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), 1)
        intent = True
        if unstoppable:
            with pytest.raises(WorkerExecutionUnstoppable):
                await asyncio.wait_for(running, 1)
            assert not acknowledgements and abandonments == [True]
            with pytest.raises(RuntimeError, match="unavailable"):
                await worker.run_once()
        else:
            assert await asyncio.wait_for(running, 1)
            assert acknowledgements == [True] and not abandonments

    asyncio.run(scenario())
