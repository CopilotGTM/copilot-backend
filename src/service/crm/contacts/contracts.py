"""Shared contact-service contracts for API, agent, workflow, and deal callers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact

ContactMatchedBy = Literal["email", "explicit_id", "created"]


@dataclass(frozen=True)
class ContactCreateCommand:
    """Canonical contact-create payload shared by API, tools, and workflows."""

    email: str
    name: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None


@dataclass(frozen=True)
class ContactWriteContext:
    """Caller metadata for future contact audit events and workflow triggers."""

    source: str
    actor_user_id: UUID | None = None
    source_label: str | None = None
    created_via: str | None = None


@dataclass(frozen=True)
class ContactWriteResult:
    """Canonical contact write result returned to API, agent, workflow, and deal callers."""

    contact: CRMContact
    created: bool
    matched_by: ContactMatchedBy
    created_account_id: UUID | None = None


@dataclass(frozen=True)
class ContactDealLinkResult:
    """Result of linking an existing contact to a deal as a participant."""

    created: list[dict]
    skipped: list[dict]
    contact_participant_changed: bool


class ContactWriteServiceError(Exception):
    """HTTP-shaped contact write error for routers and agent tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class ContactReadServiceError(Exception):
    """HTTP-shaped contact read error for routers and agent tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class ContactDealLinkServiceError(Exception):
    """HTTP-shaped contact-to-deal linking error for routers and agent tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
