"""Offline ingress smoke tests using raw ASGI and a minimal FastAPI app."""

import asyncio
import json

import pytest
from fastapi import FastAPI, Request
from pydantic import ValidationError

from domain.http_ingress import HTTPIngressPolicy
from domain.platform_runtime import PlatformRuntimeSettings
from interfaces.http.gateway_context import GatewayContext
from interfaces.http.platform_ingress import PlatformIngressMiddleware, PlatformResponseAborted


def scope(path="/v2/tasks", headers=(), method="POST"):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "headers": list(headers), "server": ("test", 80),
            "client": ("test", 123)}


async def invoke(app, messages=None, *, request_scope=None, receive=None):
    sent = []
    queue = iter(messages or [{"type": "http.request", "body": b""}])

    async def recv():
        return next(queue)

    async def send(message):
        sent.append(message.copy())

    await app(request_scope or scope(), receive or recv, send)
    return sent


def status(messages):
    return messages[0]["status"]


def test_gateway_context_matches_generated_header_after_split_body():
    admitted = []
    app = FastAPI()

    @app.post("/v2/tasks")
    async def task(request: Request, context: GatewayContext):
        admitted.append((context.request_id, await request.body()))
        return {"request_id": context.request_id}

    wrapped = PlatformIngressMiddleware(app, HTTPIngressPolicy())
    messages = asyncio.run(invoke(wrapped, [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
    ], request_scope=scope(headers=[(b"x-request-id", b"attacker"), (b"content-length", b"4")])))
    headers = dict(messages[0]["headers"])
    request_id = headers[b"x-request-id"].decode()
    assert status(messages) == 200
    assert admitted == [(request_id, b"abcd")]
    assert json.loads(messages[1]["body"])["request_id"] == request_id != "attacker"
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-content-type-options"] == b"nosniff"


def test_framing_limits_and_disconnect_never_admit():
    admitted = []

    async def app(scope, receive, send):
        admitted.append(True)

    wrapped = PlatformIngressMiddleware(app, HTTPIngressPolicy(max_body_bytes=3, max_header_bytes=80,
                                                               max_target_bytes=15))

    async def check():
        cases = [
            (scope(headers=[(b"content-length", b"4")]), None, 413),
            (scope(headers=[(b"content-length", b"1"), (b"content-length", b"1")]), None, 400),
            (scope(headers=[(b"content-length", b"1"), (b"transfer-encoding", b"chunked")]), None, 400),
            (scope(headers=[(b"content-encoding", b"gzip")]), None, 415),
            (scope(headers=[(b"x", b"a" * 80)]), None, 431),
            (scope(path="/v2/" + "x" * 20), None, 414),
            (scope(), [{"type": "http.request", "body": b"1234"}], 413),
            (scope(headers=[(b"content-length", b"2")]), [{"type": "http.request", "body": b"1"}], 400),
        ]
        for request_scope, body, expected in cases:
            result = await invoke(wrapped, body, request_scope=request_scope)
            assert status(result) == expected
            assert dict(result[0]["headers"])[b"connection"] == b"close"
        assert await invoke(wrapped, [{"type": "http.request", "body": b"a", "more_body": True},
                                      {"type": "http.disconnect"}]) == []
        assert not admitted

    asyncio.run(check())


def test_total_deadline_and_slot_release():
    admitted = []

    async def app(scope, receive, send):
        admitted.append(True)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = PlatformIngressMiddleware(app, HTTPIngressPolicy(body_read_timeout_seconds=0.08,
                                                               max_inflight_requests=1))

    async def check():
        async def slow():
            await asyncio.sleep(0.05)
            return {"type": "http.request", "body": b"a", "more_body": True}

        result = await invoke(wrapped, receive=slow)
        assert status(result) == 408
        assert not admitted
        assert status(await invoke(wrapped)) == 204
        assert admitted == [True]

    asyncio.run(check())


def test_capacity_probe_reserve_and_release_after_error():
    async def check():
        entered = asyncio.Event()
        release = asyncio.Event()
        probe_entered = asyncio.Event()
        probe_release = asyncio.Event()

        async def app(scope, receive, send):
            if scope["path"] == "/v2/tasks":
                entered.set()
                await release.wait()
                raise RuntimeError("secret database detail")
            probe_entered.set()
            await probe_release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        wrapped = PlatformIngressMiddleware(app, HTTPIngressPolicy(max_inflight_requests=1,
                                                                   reserved_probe_requests=1))
        first = asyncio.create_task(invoke(wrapped))
        await entered.wait()
        rejected = await invoke(wrapped)
        assert status(rejected) == 503
        assert dict(rejected[0]["headers"])[b"retry-after"] == b"1"
        probe = asyncio.create_task(invoke(wrapped, request_scope=scope("/livez", method="GET")))
        await probe_entered.wait()
        assert status(await invoke(wrapped, request_scope=scope("/readyz", method="GET"))) == 503
        probe_release.set()
        assert status(await probe) == 200
        assert status(await invoke(wrapped, request_scope=scope("/livez", method="GET"))) == 200
        release.set()
        failed = await first
        assert status(failed) == 500
        assert b"secret database detail" not in repr(failed).encode()
        assert status(await invoke(wrapped)) == 500  # failure released the application slot

    asyncio.run(check())


def test_streaming_late_failure_and_numeric_policy():
    async def check():
        sent = []

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"first", "more_body": True})
            assert len(sent) == 2  # delivered before the inner app continues
            raise RuntimeError("secret upstream token")

        async def send(message):
            sent.append(message.copy())

        async def receive():
            return {"type": "http.request", "body": b""}

        wrapped = PlatformIngressMiddleware(app, HTTPIngressPolicy())
        with pytest.raises(PlatformResponseAborted, match="^Platform response aborted$") as exc:
            await wrapped(scope(), receive, send)
        assert exc.value.__suppress_context__ and exc.value.__context__ is None
        assert len(sent) == 2 and sent[-1]["more_body"] is True
        assert b"secret" not in repr(sent).encode()

        async def healthy_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def blocked_send(message):
            await asyncio.sleep(1)

        slow = PlatformIngressMiddleware(healthy_app, HTTPIngressPolicy(response_send_timeout_seconds=0.01,
                                                                        max_inflight_requests=1))
        with pytest.raises(PlatformResponseAborted):
            await slow(scope(), receive, blocked_send)
        assert status(await invoke(slow)) == 204

        async def broken_app(scope, receive, send):
            raise RuntimeError("secret SQL parameters")

        async def failed_send(message):
            raise OSError("transport disconnected")

        broken = PlatformIngressMiddleware(broken_app, HTTPIngressPolicy())
        with pytest.raises(PlatformResponseAborted) as failed:
            await broken(scope(), receive, failed_send)
        assert failed.value.__context__ is None

    asyncio.run(check())
    settings = PlatformRuntimeSettings.model_validate({"http_ingress": {"max_body_bytes": "12",
                                                                         "body_read_timeout_seconds": "1.5"}})
    assert settings.http_ingress.max_body_bytes == 12
    assert settings.http_ingress.body_read_timeout_seconds == 1.5
    for value in (True, False, "NaN", "Infinity"):
        with pytest.raises(ValidationError):
            HTTPIngressPolicy.model_validate({"body_read_timeout_seconds": value})
