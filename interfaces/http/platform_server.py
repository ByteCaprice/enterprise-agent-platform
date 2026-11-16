"""HTTP adapter for headless process services; contracts export independently."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from infra.rate_limiter import RateLimiter
from interfaces.http.api_contract import API_TITLE, API_VERSION, api_operation_id, include_api_routes
from interfaces.http.platform_errors import http_exception_handler
from interfaces.http.platform_ingress import PlatformIngressMiddleware
from interfaces.runtime import RuntimeServices, create_runtime


def create_platform_app(config_dir: str | Path | None = None, *, runtime: RuntimeServices | None = None,
                        include_reference_console: bool = False) -> FastAPI:
    """Wrap services, taking ownership until shutdown (or failed app construction)."""
    if type(include_reference_console) is not bool:
        raise ValueError("Reference console selection must be explicit")
    if (config_dir is None) == (runtime is None):
        raise ValueError("Pass explicit configuration or runtime services, not both")
    services = runtime if runtime is not None else create_runtime(config_dir)
    try:
        return _create_http_app(services, include_reference_console=include_reference_console)
    except BaseException:
        services.close()
        raise


def _create_http_app(services: RuntimeServices, *, include_reference_console: bool) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            services.close()

    app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=lifespan, generate_unique_id_function=api_operation_id)
    app.state.runtime = services
    app.state.store = services.store
    app.state.control_plane_auth = services.control_plane_auth
    app.state.rate_limiter = RateLimiter({"enabled": True, "default": {"capacity": 100, "fill_rate": 10}})
    app.state.bootstrap = services.bootstrap
    app.state.platform_runtime = services.runtime_settings
    app.add_exception_handler(HTTPException, http_exception_handler)

    app.add_middleware(PlatformIngressMiddleware, policy=services.runtime_settings.http_ingress)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _exc):
        # Do not echo or log rejected prompt content, credentials or customer input.
        return JSONResponse(status_code=422, headers={"Cache-Control": "no-store"}, content={
            "error": {"code": "VALIDATION_ERROR", "message": "Request validation failed",
                      "request_id": getattr(_request.state, "request_id", None)},
        })

    @app.exception_handler(Exception)
    async def internal_error(_request, _exc):
        request_id = getattr(_request.state, "request_id", uuid4().hex)
        # No traceback, raw URL, body, SQL parameters or driver exception text.
        logging.getLogger("enterprise_ai.http").error("request_failed id=%s exception_type=%s",
                                                    request_id, type(_exc).__name__)
        return JSONResponse(status_code=500, headers={"Cache-Control": "no-store", "X-Request-ID": request_id}, content={
            "error": {"code": "INTERNAL_ERROR", "message": "Platform operation failed", "request_id": request_id},
        })

    include_api_routes(app)
    if include_reference_console:
        from interfaces.http.routes.console import router as console_router

        app.include_router(console_router)

    @app.get("/livez")
    def livez():
        return {"status": "alive", "service": "enterprise-ai-platform"}

    @app.get("/readyz")
    def readyz():
        try:
            services.check_ready()
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready", "service": "enterprise-ai-platform"}

    return app
