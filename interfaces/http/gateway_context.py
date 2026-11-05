"""Transport-owned IDs, separate from client correlation and authenticated identity."""

from typing import Annotated

from fastapi import Depends, Header, Request, Response

from domain.gateway import CorrelationId, GatewayRequestContext


def gateway_request_context(
    request: Request,
    x_correlation_id: Annotated[CorrelationId | None, Header(
        description="Optional opaque correlation token; never an identity or idempotency key",
    )] = None,
) -> GatewayRequestContext:
    request_id = getattr(request.state, "request_id", None)
    context = (GatewayRequestContext(correlation_id=x_correlation_id) if request_id is None else
               GatewayRequestContext(request_id=request_id, correlation_id=x_correlation_id))
    request.state.request_id = context.request_id
    return context


GatewayContext = Annotated[GatewayRequestContext, Depends(gateway_request_context)]


def gateway_response_headers(response: Response, context: GatewayRequestContext) -> None:
    response.headers["X-Request-ID"] = context.request_id
    if context.correlation_id is not None:
        response.headers["X-Correlation-ID"] = context.correlation_id
