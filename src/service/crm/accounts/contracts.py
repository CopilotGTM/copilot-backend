"""Shared account-service contracts for API, agent, workflow, and deal callers.

The read/write services intentionally expose HTTP-shaped errors because routers
and agent tools need the same status/message/data payload without importing
FastAPI primitives into the service layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany

# These literals document the duplicate policy vocabulary before every caller has
# migrated to the new account module. Not every mode is used in this first slice.
AccountCreateMode = Literal["strict", "resolve_or_create"]
AccountDuplicateScope = Literal["strict_org", "internal_only", "resolve_or_create"]
AccountMatchedBy = Literal[
    "domain",
    "name",
    "linkedin",
    "apollo",
    "explicit_id",
    "copied_source",
    "created",
]


@dataclass(frozen=True)
class AccountCreateCommand:
    """Canonical account-create payload shared by API, tools, and workflows."""

    name: str | None = None
    domain: str | None = None
    website: str | None = None
    industry: str | None = None
    linkedin_url: str | None = None


@dataclass(frozen=True)
class AccountWriteContext:
    """Caller metadata for duplicate policy and future account audit events."""

    source: str
    actor_user_id: UUID | None = None
    actor_name: str | None = None
    actor_email: str | None = None
    source_label: str | None = None
    created_via: str | None = None
    duplicate_scope: AccountDuplicateScope = "internal_only"


@dataclass(frozen=True)
class AccountWriteResult:
    """Canonical account write result returned to API, agent, workflow, and deal callers."""

    account: CRMCompany
    created: bool
    matched_by: AccountMatchedBy


class AccountWriteServiceError(Exception):
    """HTTP-shaped account write error for routers and agent tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class AccountReadServiceError(Exception):
    """HTTP-shaped account read error for routers and agent tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
