"""Base types for webhook auth validators."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AuthValidationResult:
    """Result of validating a webhook authentication payload."""

    is_valid: bool
    error: str | None = None
    payload: dict[str, Any] | None = None


class WebhookAuthValidator(Protocol):
    """Protocol for webhook authentication validators."""

    async def validate(
        self, headers: Mapping[str, str], body: bytes | None = None
    ) -> AuthValidationResult:
        """Validate webhook request headers/body."""
        raise NotImplementedError
