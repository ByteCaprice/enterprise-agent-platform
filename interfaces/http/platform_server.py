"""Runtime application composition; API contracts can be exported independently."""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException

from infra.database import build_engine
from infra.deployment_policy import validate_deployment
from infra.platform_migrations import PLATFORM_SCHEMA_REVISION
from infra.platform_store import PlatformStore
from infra.rate_limiter import RateLimiter
from infra.schema_validation import check_validation_backend
from interfaces.http.api_contract import API_TITLE, API_VERSION, api_operation_id, include_api_routes
from interfaces.http.platform_errors import http_exception_handler
from interfaces.http.platform_ingress import PlatformIngressMiddleware
from interfaces.platform_settings import SettingsValidationError, load_settings
from orchestration.control_plane_auth import ControlPlaneAuth


def create_platform_app(config_dir: str | Path, *, include_reference_console: bool = False) -> FastAPI:
    if type(include_reference_console) is not bool:
        raise ValueError("Reference console selection must be explicit")
    bootstrap = load_settings(Path(config_dir).resolve())
    runtime_settings = validate_deployment(bootstrap, config_dir)
    database = dict(bootstrap.get("database", {}))
    for name, value in {"statement_timeout_ms": 15000, "lock_timeout_ms": 5000, "pool_timeout_seconds": 10}.items():
        if database.get(name) is None:
            database[name] = value
    schema = database.get("schema")
    if not schema or schema in {"public", "pg_catalog", "information_schema"}:
        raise SettingsValidationError("New platform requires an explicit, isolated DATABASE__SCHEMA")
    store = PlatformStore(build_engine(database))
    try:
        check_validation_backend()
        with store.engine.connect() as connection:
            actual = connection.scalar(text("SELECT current_schema()"))
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if actual != schema or revision != PLATFORM_SCHEMA_REVISION:
            raise SettingsValidationError("Database is not a supported new-platform schema")
        keys = store.api_key_configs.list()
        if not keys or any(re.fullmatch(r"sha256:[a-f0-9]{64}", key.api_key) is None for key in keys):
            raise SettingsValidationError("New platform requires digest-backed API keys")
    except Exception:
        store.close()
        raise

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=lifespan, generate_unique_id_function=api_operation_id)
    app.state.store = store
    app.state.control_plane_auth = ControlPlaneAuth(store.api_key_configs, store.governance, store.oidc)
    app.state.rate_limiter = RateLimiter({"enabled": True, "default": {"capacity": 100, "fill_rate": 10}})
    app.state.bootstrap = bootstrap
    app.state.platform_runtime = runtime_settings
    app.add_exception_handler(HTTPException, http_exception_handler)

    app.add_middleware(PlatformIngressMiddleware, policy=runtime_settings.http_ingress)

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
            with store.engine.connect() as connection:
                if connection.scalar(text("SELECT current_schema()")) != schema:
                    raise RuntimeError("Schema isolation lost")
                if connection.scalar(text("SELECT version_num FROM alembic_version")) != PLATFORM_SCHEMA_REVISION:
                    raise RuntimeError("Unsupported schema revision")
            store.capabilities.list("platform", limit=1)
            store.governance.get_project("platform")
            store.governance.check_ready()
            store.tasks.check_ready()
            store.models.check_ready()
            store.tool_bindings.check_ready()
            store.agents.check_ready()
            store.task_events.check_ready()
            store.gateway_requests.check_ready()
            store.operations.check_ready()
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready", "service": "enterprise-ai-platform"}

    return app
