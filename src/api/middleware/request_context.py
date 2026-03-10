from __future__ import annotations

import time

from src.infra.observability.context import generate_request_id, reset_context, set_request_id
from src.infra.utils.logger import get_logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_logger = get_logger("api.access")

# Paths that generate too much noise at INFO level
_QUIET_PATHS = frozenset({"/health", "/healthz", "/ready"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Request correlation ID + structured access logging.

    - Propagates/generates X-Request-Id
    - Logs every request with method, path, status, and duration
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id")
        if not request_id:
            request_id = generate_request_id()

        set_request_id(request_id)
        request.state.request_id = request_id

        start = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = request_id

            duration_ms = round((time.perf_counter() - start) * 1000)
            self._log_request(request, response.status_code, duration_ms)

            return response
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000)
            self._log_request(request, 500, duration_ms)
            raise
        finally:
            reset_context()

    @staticmethod
    def _log_request(request: Request, status: int, duration_ms: int) -> None:
        path = request.url.path
        method = request.method
        extra = {
            "event": "http_request",
            "http_method": method,
            "http_path": path,
            "http_status_code": status,
            "duration_ms": duration_ms,
        }

        if path in _QUIET_PATHS:
            _logger.debug("HTTP request completed", extra=extra)
        elif status >= 500:
            _logger.error("HTTP request completed", extra=extra)
        elif status >= 400:
            _logger.warning("HTTP request completed", extra=extra)
        else:
            _logger.info("HTTP request completed", extra=extra)
