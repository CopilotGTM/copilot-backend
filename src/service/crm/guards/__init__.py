"""CRM guard utilities.

Business-rule guardrails used by agent tools, services, and workers
to enforce data integrity constraints in the CRM domain.
"""

from src.service.crm.guards.internal_user_guard import (
    FREE_EMAIL_DOMAINS,
    is_internal_email,
)

__all__ = [
    "FREE_EMAIL_DOMAINS",
    "is_internal_email",
]
