"""Compatibility imports for the email sync service.

The implementation lives in `src.service.integrations.email.sync_service`.
Keep this module so existing workers, tests, and external imports do not have
to move in the same refactor.
"""

from src.service.integrations.email.normalizer import parse_email_address, parse_email_list
from src.service.integrations.email.sync_service import EmailSyncService


def _parse_email_address(raw: str) -> tuple[str, str | None]:
    """Compatibility wrapper for tests and older call sites."""
    return parse_email_address(raw)


def _parse_email_list(header_value: str | None) -> list[str]:
    """Compatibility wrapper for tests and older call sites."""
    return parse_email_list(header_value)


__all__ = [
    "EmailSyncService",
    "_parse_email_address",
    "_parse_email_list",
]
