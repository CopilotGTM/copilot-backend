from __future__ import annotations

from src.infra.observability.context import generate_request_id, reset_context, set_request_id
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Adds/propagates a request correlation ID and stores it in ContextVars.

    - Reads `X-Request-Id` (or `X-Correlation-Id`) from the incoming request
    - Generates one if missing
    - Sets it in the response header as `X-Request-Id`
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id")
        if not request_id:
            request_id = generate_request_id()

        set_request_id(request_id)
        request.state.request_id = request_id

        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            reset_context()
