"""Bound HTTP input before parsing; preserve response streaming and safe error boundaries."""

from __future__ import annotations

import asyncio
import logging
from threading import Lock
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domain.http_ingress import HTTPIngressPolicy

logger = logging.getLogger("enterprise_ai.http")


class PlatformResponseAborted(RuntimeError):
    """Only this safe exception may reach the server after response headers were sent."""


class _RejectedRequest(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class PlatformIngressMiddleware:
    def __init__(self, app: ASGIApp, policy: HTTPIngressPolicy):
        self.app = app
        self.policy = policy
        self._lock = Lock()
        self._active = {"application": 0, "probe": 0}

    def _reserve(self, bucket: str) -> bool:
        limit = self.policy.reserved_probe_requests if bucket == "probe" else self.policy.max_inflight_requests
        with self._lock:
            if self._active[bucket] >= limit:
                return False
            self._active[bucket] += 1
            return True

    def _release(self, bucket: str) -> None:
        with self._lock:
            self._active[bucket] -= 1

    def _framing(self, scope: Scope) -> int | None:
        target = scope.get("raw_path", scope["path"].encode("utf-8")) + scope.get("query_string", b"")
        if len(target) > self.policy.max_target_bytes:
            raise _RejectedRequest(414, "REQUEST_TARGET_TOO_LONG", "Request target exceeds size limit")
        total = 0
        lengths = []
        transfer = False
        for key, value in scope.get("headers", []):
            total += len(key) + len(value) + 4
            if total > self.policy.max_header_bytes:
                raise _RejectedRequest(431, "REQUEST_HEADERS_TOO_LARGE", "Request headers exceed size limit")
            key = key.lower()
            if key == b"content-length":
                lengths.append(value.strip())
            elif key == b"transfer-encoding":
                transfer = True
            elif key == b"content-encoding" and value.strip().lower() != b"identity":
                raise _RejectedRequest(415, "REQUEST_CONTENT_ENCODING_UNSUPPORTED", "Compressed request bodies are not supported")
        if len(lengths) > 1 or (lengths and transfer) or (
            lengths and (not lengths[0].isdigit() or len(lengths[0]) > 20)
        ):
            raise _RejectedRequest(400, "REQUEST_FRAMING_INVALID", "Invalid request framing")
        size = int(lengths[0]) if lengths else None
        if size is not None and size > self.policy.max_body_bytes:
            raise _RejectedRequest(413, "REQUEST_BODY_TOO_LARGE", "Request body exceeds size limit")
        return size

    async def _body(self, receive: Receive, declared: int | None) -> bytes | None:
        body = bytearray()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.policy.body_read_timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    if loop.time() >= deadline:
                        raise TimeoutError
                    message = await receive()
                    if loop.time() >= deadline:
                        raise TimeoutError
                    if message["type"] == "http.disconnect":
                        return None
                    if message["type"] != "http.request":
                        raise _RejectedRequest(400, "REQUEST_FRAMING_INVALID", "Invalid request framing")
                    chunk = message.get("body", b"")
                    size = len(body) + len(chunk)
                    if size > self.policy.max_body_bytes:
                        raise _RejectedRequest(413, "REQUEST_BODY_TOO_LARGE", "Request body exceeds size limit")
                    if declared is not None and size > declared:
                        raise _RejectedRequest(400, "REQUEST_FRAMING_INVALID", "Invalid request framing")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            raise _RejectedRequest(408, "REQUEST_BODY_TIMEOUT", "Request body read timed out") from None
        if declared is not None and len(body) != declared:
            raise _RejectedRequest(400, "REQUEST_FRAMING_INVALID", "Invalid request framing")
        return bytes(body)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        bucket = "probe" if scope["path"] in {"/livez", "/readyz"} and scope["method"] in {"GET", "HEAD"} else "application"
        reserved = False
        started = False
        aborted = False
        status = 499

        async def safe_send(message: Message) -> None:
            nonlocal started, status
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                if scope["path"].startswith("/v2/"):
                    headers["Cache-Control"] = "no-store"
                    headers["X-Content-Type-Options"] = "nosniff"
                status = message["status"]
                started = True
            try:
                async with asyncio.timeout(self.policy.response_send_timeout_seconds):
                    await send(message)
            except TimeoutError:
                raise PlatformResponseAborted("Platform response aborted") from None

        async def reject(rejection: _RejectedRequest) -> bool:
            headers = {"Cache-Control": "no-store"}
            if str(scope.get("http_version", "1.1")).startswith("1"):
                headers["Connection"] = "close"
            if rejection.status == 503:
                headers["Retry-After"] = "1"
            response = JSONResponse(status_code=rejection.status, headers=headers, content={
                "error": {"code": rejection.code, "message": rejection.message, "request_id": request_id},
            })
            try:
                await response(scope, receive, safe_send)
            except Exception:
                return False
            return True

        try:
            declared = self._framing(scope)
            reserved = self._reserve(bucket)
            if not reserved:
                raise _RejectedRequest(503, "HTTP_CAPACITY_EXHAUSTED", "Platform HTTP capacity is exhausted")
            body = await self._body(receive, declared)
            if body is None:
                return
            delivered = False

            async def buffered_receive() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, buffered_receive, safe_send)
        except _RejectedRequest as rejection:
            aborted = started or not await reject(rejection)
        except Exception as exc:
            # Never expose driver/SDK exception text or a traceback with its cause.
            logger.error("request_failed id=%s exception_type=%s", request_id, type(exc).__name__)
            aborted = started or not await reject(_RejectedRequest(500, "INTERNAL_ERROR", "Platform operation failed"))
        finally:
            if reserved:
                self._release(bucket)
            logger.info("request id=%s method=%s route=%s status=%s", request_id, scope["method"],
                        getattr(scope.get("route"), "path", "unmatched"), status)
        if aborted:
            # Raise OUTSIDE the except blocks: even __context__ must not retain
            # a credential/customer-bearing SDK or SQL exception for the server.
            raise PlatformResponseAborted("Platform response aborted") from None
