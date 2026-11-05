"""Safe platform error envelopes; never expose raw exception text."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

_HTTP_ERRORS = {
    400: ("BAD_REQUEST", "Invalid request"),
    401: ("UNAUTHORIZED", "Authentication required"),
    403: ("FORBIDDEN", "Resource access denied"),
    404: ("NOT_FOUND", "Resource not found"),
    405: ("METHOD_NOT_ALLOWED", "Method not allowed"),
    409: ("CONFLICT", "Request conflicts with existing state"),
    413: ("PAYLOAD_TOO_LARGE", "Request exceeds size limit"),
    422: ("VALIDATION_ERROR", "Request validation failed"),
    429: ("RATE_LIMITED", "Too many requests"),
    500: ("INTERNAL_ERROR", "Platform operation failed"),
    502: ("BAD_GATEWAY", "Upstream operation failed"),
    503: ("SERVICE_UNAVAILABLE", "Platform temporarily unavailable"),
}


async def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    code, message = _HTTP_ERRORS.get(status, ("HTTP_ERROR", "Request failed"))
    headers = dict(exc.headers or {}) if isinstance(exc, HTTPException) else {}
    headers["Cache-Control"] = "no-store"
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message,
        "request_id": getattr(_request.state, "request_id", None)}}, headers=headers)
