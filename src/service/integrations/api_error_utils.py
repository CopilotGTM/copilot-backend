"""Shared API error helpers for integration providers/workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

_INSUFFICIENT_SCOPE_PATTERNS = (
    "missing_scope",
    "insufficient_scope",
    "insufficientpermissions",
    "insufficient permissions",
    "insufficient privileges",
    "insufficient privilege",
    "insufficient authentication scopes",
    "access_token_scope_insufficient",
    "scope not authorized",
    "required application permissions",
)


@dataclass(frozen=True)
class ApiErrorDetails:
    status_code: int | None
    summary: str


class IntegrationPermissionError(Exception):
    """Raised when an API call fails because the integration lacks required permissions."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider: str | None = None,
        endpoint: str | None = None,
        response_summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider
        self.endpoint = endpoint
        self.response_summary = response_summary


def _truncate_summary(text: str, limit: int = 300) -> str:
    compact = " ".join(text.split())
    return compact[:limit]


def _response_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        message = payload.get("message")
        if isinstance(message, str):
            return message
    return response.text


def extract_api_error_details(exc: Exception) -> ApiErrorDetails:
    if isinstance(exc, IntegrationPermissionError):
        summary = exc.response_summary or str(exc)
        return ApiErrorDetails(status_code=exc.status_code, summary=_truncate_summary(summary))

    if isinstance(exc, httpx.HTTPStatusError):
        return ApiErrorDetails(
            status_code=exc.response.status_code,
            summary=_truncate_summary(_response_error_message(exc.response) or str(exc)),
        )

    return ApiErrorDetails(status_code=None, summary=_truncate_summary(str(exc)))


def is_insufficient_scope_error(exc: Exception) -> bool:
    if isinstance(exc, IntegrationPermissionError):
        return True

    details = extract_api_error_details(exc)
    haystack = details.summary.lower()

    if details.status_code == 403 and any(
        pattern in haystack for pattern in _INSUFFICIENT_SCOPE_PATTERNS
    ):
        return True

    return any(pattern in haystack for pattern in _INSUFFICIENT_SCOPE_PATTERNS)


def log_provider_api_error(
    logger: Any,
    *,
    provider: str,
    operation: str,
    exc: Exception,
    endpoint: str | None = None,
    **context: object,
) -> None:
    details = extract_api_error_details(exc)
    logger.warning(
        "%s API %s failed: status=%s endpoint=%s detail=%s",
        provider,
        operation,
        details.status_code,
        endpoint,
        details.summary,
        extra=context or None,
    )
