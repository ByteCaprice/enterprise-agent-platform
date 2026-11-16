"""Keep synchronous execution ports owned until their thread has stopped."""

import asyncio

from domain.task_execution import (
    ExecutionFailure,
    ExecutionResourceUnstoppable,
    TaskCancellationRequested,
    TaskLeaseLost,
)


async def run_sync(function, *args, _on_cancel_result=None, _on_cancel_error=None, **kwargs):
    """Drain even repeated cancellation, then rethrow it (or a late port failure).

    The Worker's cancellation grace handles an uncooperative thread. Returning
    early would release execution authority while that thread still owns IO.
    Callbacks are synchronous and may retain committed IDs or expose uncertainty.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not task.cancelled():
            failure = task.exception()
            if failure is not None and _on_cancel_error is not None:
                _on_cancel_error(failure)
            if failure is None and _on_cancel_result is not None:
                _on_cancel_result(task.result())
        raise


def accounting_error(code):
    """A drained transaction error is not proof that its commit did not happen."""
    def failed(failure):
        if isinstance(failure, (TaskLeaseLost, TaskCancellationRequested, ExecutionResourceUnstoppable)):
            raise failure
        if isinstance(failure, ExecutionFailure) and failure.code.endswith("ACCOUNTING_RESULT_UNKNOWN"):
            raise failure
        raise ExecutionFailure(code) from None

    return failed
