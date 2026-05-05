"""Per-form CORS enforcement for public lead form endpoints.

The public lead form route namespace is intentionally reachable by third-party sites.
This middleware reads the allow-list for the exact public form key from master
storage and mirrors the CORS headers needed for form embeds and preflight checks
without relaxing the global auth CORS policy.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.master_tables import (
    PublicIngressKey,
    PublicIngressKeyStatus,
    PublicIngressKeyType,
    PublicIngressResourceType,
)
from src.infra.database.sessions import acquire_master_async_session
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_PUBLIC_FORM_PATH_RE = re.compile(
    r"^/public/v1/forms/(?P<public_form_key>lf_[A-Za-z0-9]+)(?:/submissions)?/?$"
)
_PUBLIC_FORM_ALLOWED_METHODS = "GET, POST, OPTIONS"


async def resolve_public_form_allowed_origins(public_form_key: str) -> list[str]:
    """Return the active origin allow-list for one public lead form key.

    This lookup runs in the master schema because public form ingress keys are
    global tenant metadata. The method only returns active form entries so disabled
    forms effectively become closed (no CORS allow-list).
    """
    # Keep this lookup to one simple DB round-trip so per-request CORS checks stay
    # fast and avoid introducing cross-schema routing or tenant-level lookups.
    async for session in acquire_master_async_session():
        stmt = select(PublicIngressKey).where(
            col(PublicIngressKey.public_key) == public_form_key,
            col(PublicIngressKey.key_type) == PublicIngressKeyType.PUBLIC_FORM.value,
            col(PublicIngressKey.resource_type) == PublicIngressResourceType.LEAD_FORM.value,
            col(PublicIngressKey.status) == PublicIngressKeyStatus.ACTIVE.value,
        )
        ingress_key = (await session.execute(stmt)).scalars().first()
        if not ingress_key:
            return []
        return [origin for origin in ingress_key.allowed_origins if isinstance(origin, str)]
    return []


class PublicLeadFormCORSMiddleware:
    """Reflect per-form CORS policy for public form requests.

    The middleware is intentionally inserted after the auth CORS middleware so
    it can enforce a stricter per-form allow-list before normal request routing.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Skip non-HTTP traffic entirely. This keeps websocket and background
        # scopes untouched and avoids mutating non-HTTP message formats.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        public_form_key = self._public_form_key(scope)
        if not public_form_key:
            await self.app(scope, receive, send)
            return

        request_headers = Headers(scope=scope)
        origin = request_headers.get("origin")
        # No Origin means this is not a browser-driven CORS request path. We pass
        # through so non-browser clients and health probes keep working unchanged.
        if not origin:
            await self.app(scope, receive, send)
            return

        allowed_origins = await resolve_public_form_allowed_origins(public_form_key)
        if origin not in allowed_origins:
            # If an origin is not in the allow-list, block preflight early with
            # a 403 to stop browser requests before app routing; normal requests
            # continue to normal auth CORS handling so we don't accidentally hide
            # app-level errors behind a synthetic response.
            if self._is_preflight(scope, request_headers):
                response = PlainTextResponse("CORS origin not allowed", status_code=403)
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        if self._is_preflight(scope, request_headers):
            # Reflect only the browser-required pieces. This intentionally avoids
            # touching auth/session middleware for OPTIONS preflights, because
            # those are browser capability checks rather than full API calls.
            response = Response(
                status_code=204, headers=self._cors_headers(origin, request_headers)
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, self._actual_response_sender(send, origin))

    @staticmethod
    def _public_form_key(scope: Scope) -> str | None:
        match = _PUBLIC_FORM_PATH_RE.match(str(scope.get("path", "")))
        return match.group("public_form_key") if match else None

    @staticmethod
    def _is_preflight(scope: Scope, headers: Headers) -> bool:
        return scope["method"] == "OPTIONS" and "access-control-request-method" in headers

    @staticmethod
    def _cors_headers(origin: str, request_headers: Headers) -> dict[str, str]:
        # Keep this header set narrow and explicit; OPTIONS responses should only
        # advertise what this middleware allows for the route to avoid over-broad
        # CORS grants that are hard to revoke.
        headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": _PUBLIC_FORM_ALLOWED_METHODS,
            "Vary": "Origin",
        }
        requested_headers = request_headers.get("access-control-request-headers")
        if requested_headers:
            headers["Access-Control-Allow-Headers"] = requested_headers
        return headers

    def _actual_response_sender(
        self, send: Send, origin: str
    ) -> Callable[[Message], Awaitable[None]]:
        # Inject CORS response header only on response start, and only after the
        # upstream app/router has decided to handle the request. This preserves
        # existing response semantics while still applying browser permission.
        async def send_with_cors(message: Message) -> None:
            """Inject the allow-origin header for responses that are allowed."""
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Access-Control-Allow-Origin"] = origin
                headers.add_vary_header("Origin")
            await send(message)

        return send_with_cors
