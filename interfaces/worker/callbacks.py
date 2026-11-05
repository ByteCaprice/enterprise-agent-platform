"""Explicit separate delivery worker; retries delivery only, never a Task."""

import asyncio
from uuid import uuid4

from infra.callback_http import CallbackHTTPTransport
from infra.model_credentials import EnvironmentCredentialResolver
from interfaces.worker.lifecycle import observe_worker, stop_signals


async def run_callback_worker(store, *, once=False, transport=None, credentials=None, worker_id=None):
    transport = transport or CallbackHTTPTransport()
    credentials = credentials or EnvironmentCredentialResolver()
    stop = asyncio.Event()

    async def run():
        while not stop.is_set():
            claimed = await deliver_once()
            if once:
                return claimed
            if not claimed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
        return None

    async def deliver_once():
        claimed = await asyncio.to_thread(store.callbacks.claim)
        if claimed is not None:
            delivery, endpoint = claimed
            try:
                secret = credentials.resolve(delivery["project_key"], endpoint["credential_ref"])
                status = await transport.send(delivery, endpoint, secret)
                error = None if 200 <= status < 300 else "CALLBACK_HTTP_REJECTED"
            except Exception:
                # Do not store exception strings, URLs with credentials or response bodies.
                status, error = None, "CALLBACK_DELIVERY_RESULT_UNKNOWN"
            await asyncio.to_thread(store.callbacks.finish, delivery["id"], delivery["lease_token"],
                                    http_status=status, error_code=error)
        return claimed is not None

    with stop_signals(stop):
        return await observe_worker(store, worker_id or f"callback-{uuid4().hex}", "callback", run)
