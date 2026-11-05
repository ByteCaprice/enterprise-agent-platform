"""Shared API topology and schema export, without process or database initialization."""

import re

from fastapi import FastAPI
from fastapi.routing import APIRoute

from interfaces.http.api_models import APIErrorResponse
from interfaces.http.routes.capabilities import router as capability_router
from interfaces.http.routes.data_retention import router as data_retention_router
from interfaces.http.routes.executions import router as execution_router
from interfaces.http.routes.gateway import router as gateway_router
from interfaces.http.routes.governance import router as governance_router
from interfaces.http.routes.models import router as model_router
from interfaces.http.routes.platform_operations import router as operations_router
from interfaces.http.routes.session import router as session_router
from interfaces.http.routes.tasks import router as task_router
from interfaces.http.routes.tools import router as tool_router

API_TITLE = "Enterprise AI Platform"
API_VERSION = "2.0"

_ERROR_STATUSES = {
    400: "Invalid request", 401: "Authentication required", 403: "Resource access denied", 404: "Resource not found",
    405: "Method not allowed", 408: "Request body timed out", 409: "State or idempotency conflict",
    413: "Request body too large", 414: "Request target too long", 415: "Unsupported content encoding",
    422: "Request validation failed", 429: "Rate limited", 431: "Request headers too large",
    500: "Platform operation failed", 502: "Upstream operation failed", 503: "Temporarily unavailable",
}


def api_operation_id(route: APIRoute) -> str:
    """Stable across Python handler renames; derived from the HTTP contract."""
    path = re.sub(r"[^a-zA-Z0-9]+", "_", route.path_format).strip("_")
    return f"{sorted(route.methods)[0].lower()}_{path}"


def include_api_routes(app: FastAPI) -> None:
    errors = {status: {"model": APIErrorResponse, "description": description} for status, description in _ERROR_STATUSES.items()}
    for router in (session_router, capability_router, governance_router, model_router, task_router,
                   execution_router, gateway_router, tool_router, operations_router, data_retention_router):
        app.include_router(router, responses=errors)


def export_openapi() -> dict:
    """Build a document, not a runnable application with fake auth or fake storage.

    No configuration, credentials, Store, runtime adapters or network are created.
    Runtime /openapi.json uses exactly the same business routes and models.
    """
    app = FastAPI(title=API_TITLE, version=API_VERSION, generate_unique_id_function=api_operation_id)
    include_api_routes(app)
    return app.openapi()
