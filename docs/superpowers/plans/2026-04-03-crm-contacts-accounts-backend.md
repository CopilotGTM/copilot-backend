# CRM Contacts & Accounts — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add backend API endpoints for Contacts and Accounts CRUD, with auto account resolution from email domain, enrichment at the contact/company level, and "add contact to deal" functionality.

**Architecture:** New services (`ContactsAccessService`, `AccountsAccessService`) follow the existing read-service pattern (session + org_id via DI). New handler files register under `/users/crm/contacts` and `/users/crm/accounts`. An org migration adds research columns to `crm_contacts` and `crm_companies`.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy async, Alembic (org migrations), Pydantic v2, pytest

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `migrations/org_migrations/versions/3c7f79637798_030_add_contact_company_research_fields.py` | Alembic migration adding research columns |
| `src/api/schemas/crm_contact_models.py` | Pydantic request/response schemas for contacts |
| `src/api/schemas/crm_account_models.py` | Pydantic request/response schemas for accounts |
| `src/service/crm/access/contacts_access_service.py` | Contacts list, detail, create, update, enrich, add-to-deal |
| `src/service/crm/access/accounts_access_service.py` | Accounts list, detail, create, update, enrich |
| `src/api/routers/crm/contacts_handler.py` | FastAPI routes for `/users/crm/contacts` |
| `src/api/routers/crm/accounts_handler.py` | FastAPI routes for `/users/crm/accounts` |
| `tests/service/crm/test_contacts_access_service.py` | Unit tests for contacts service |
| `tests/service/crm/test_accounts_access_service.py` | Unit tests for accounts service |

### Modified Files

| File | Change |
|------|--------|
| `src/domain/models/organisation_tables/sync/crm/contacts.py` | Add `contact_research`, `last_researched_at`, `research_status` fields |
| `src/domain/models/organisation_tables/sync/crm/companies.py` | Add `company_research`, `last_researched_at`, `research_status` fields |
| `src/api/routers/crm/__init__.py` | Import and export new routers |
| `src/api/fastapi_app.py` | Register new routers with `include_router` |

---

## Task 1: Database Migration — Add Research Fields

**Files:**
- Create: `migrations/org_migrations/versions/3c7f79637798_030_add_contact_company_research_fields.py`
- Modify: `src/domain/models/organisation_tables/sync/crm/contacts.py`
- Modify: `src/domain/models/organisation_tables/sync/crm/companies.py`

- [ ] **Step 1: Create the Alembic migration file**

```python
"""Add research fields to crm_contacts and crm_companies.

Revision ID: 3c7f79637798
Revises: 3c7f79637797
Create Date: 2026-04-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637798"
down_revision: str | None = "3c7f79637797"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # crm_contacts: research fields
    op.add_column("crm_contacts", sa.Column("contact_research", sa.Text(), nullable=True))
    op.add_column(
        "crm_contacts",
        sa.Column("last_researched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_contacts",
        sa.Column("research_status", sa.String(length=20), nullable=True),
    )

    # crm_companies: research fields
    op.add_column("crm_companies", sa.Column("company_research", sa.Text(), nullable=True))
    op.add_column(
        "crm_companies",
        sa.Column("last_researched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_companies",
        sa.Column("research_status", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("crm_companies", "research_status")
    op.drop_column("crm_companies", "last_researched_at")
    op.drop_column("crm_companies", "company_research")
    op.drop_column("crm_contacts", "research_status")
    op.drop_column("crm_contacts", "last_researched_at")
    op.drop_column("crm_contacts", "contact_research")
```

- [ ] **Step 2: Add fields to CRMContact model**

In `src/domain/models/organisation_tables/sync/crm/contacts.py`, add these fields to the `CRMContact` class (after the existing `data_payload` field):

```python
    # Research / enrichment
    contact_research: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    last_researched_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    research_status: str | None = Field(
        default=None, sa_column=Column(String(20), nullable=True)
    )
```

Also add the needed imports at the top: `Text`, `DateTime`, `String` from `sqlalchemy` (check which are already imported).

- [ ] **Step 3: Add fields to CRMCompany model**

In `src/domain/models/organisation_tables/sync/crm/companies.py`, add these fields to the `CRMCompany` class (after the existing `data_payload` field):

```python
    # Research / enrichment
    company_research: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    last_researched_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    research_status: str | None = Field(
        default=None, sa_column=Column(String(20), nullable=True)
    )
```

- [ ] **Step 4: Verify migration file is valid**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run python -c "import migrations.org_migrations.versions as v; print('import ok')"`

Expected: No import errors.

---

## Task 2: Contacts Pydantic Schemas

**Files:**
- Create: `src/api/schemas/crm_contact_models.py`

- [ ] **Step 1: Create the contacts schema file**

```python
"""Pydantic schemas for CRM Contacts API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ContactListItem(BaseModel):
    """Single contact in a list response."""

    contact_id: UUID
    external_id: str
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    email: str | None = None
    job_title: str | None = None
    phone: str | None = None
    company_name: str | None = None
    company_id: UUID | None = Field(None, description="Linked CRMCompany ID (if resolved)")
    company_domain: str | None = None
    lifecycle_stage: str | None = None
    owner_name: str | None = None
    deal_count: int = Field(0, description="Number of deals this contact is a stakeholder on")
    source: str = Field(
        "crm_sync",
        description="How the contact was created: crm_sync, internal, calendar_discovery, email_discovery",
    )
    research_status: str | None = Field(None, description="pending / completed / failed")
    last_researched_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContactDetail(ContactListItem):
    """Extended contact detail with deal involvement."""

    additional_emails: list[str] = Field(default_factory=list)
    mobile_phone: str | None = None
    linkedin_url: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    contact_research: str | None = Field(None, description="Markdown enrichment profile")
    last_contacted_at: datetime | None = None
    last_activity_at: datetime | None = None
    deal_involvement: list[ContactDealInvolvement] = Field(
        default_factory=list,
        description="Deals where this contact is a stakeholder",
    )


class ContactDealInvolvement(BaseModel):
    """A deal this contact participates in as a stakeholder."""

    deal_id: UUID
    deal_name: str | None = None
    deal_status: str | None = None
    stage_name: str | None = None
    stakeholder_id: UUID
    stance: str | None = None
    decision_power: str | None = None
    relationship_role: str | None = None


class ContactsPage(BaseModel):
    """Paginated contacts list."""

    items: list[ContactListItem]
    page: int = Field(..., description="Current page (1-indexed)")
    limit: int = Field(..., description="Page size")
    total_count: int = Field(..., description="Total contacts matching filters")
    total_pages: int


class CreateContactRequest(BaseModel):
    """Create a new contact. Email and name required; account auto-resolved from domain."""

    email: str = Field(..., description="Email address (required)")
    name: str = Field(..., description="Full name (required)")
    job_title: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None


class UpdateContactRequest(BaseModel):
    """Update an existing contact. All fields optional."""

    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    job_title: str | None = None
    phone: str | None = None
    mobile_phone: str | None = None
    linkedin_url: str | None = None
    company_name: str | None = None


class AddContactToDealRequest(BaseModel):
    """Add a contact as a stakeholder on a deal."""

    deal_id: UUID = Field(..., description="Target deal ID")
    relationship_role: str | None = Field(None, description="Optional role, e.g. decision_maker")


class EnrichContactResponse(BaseModel):
    """Response after triggering contact enrichment."""

    job_id: str | None = Field(None, description="Research job ID (null if skipped)")
    message: str
```

Note: `ContactDetail` references `ContactDealInvolvement` which is defined below it. Move `ContactDealInvolvement` above `ContactDetail` in the file.

- [ ] **Step 2: Verify the schema file imports cleanly**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run python -c "from src.api.schemas.crm_contact_models import ContactsPage, CreateContactRequest; print('ok')"`

Expected: `ok`

---

## Task 3: Accounts Pydantic Schemas

**Files:**
- Create: `src/api/schemas/crm_account_models.py`

- [ ] **Step 1: Create the accounts schema file**

```python
"""Pydantic schemas for CRM Accounts API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AccountListItem(BaseModel):
    """Single account/company in a list response."""

    company_id: UUID
    external_id: str
    name: str | None = None
    domain: str | None = None
    website: str | None = None
    industry: str | None = None
    lifecycle_stage: str | None = None
    owner_name: str | None = None
    number_of_employees: int | None = None
    annual_revenue: float | None = None
    phone: str | None = None
    country: str | None = None
    linkedin_url: str | None = None
    is_active: bool = True
    contact_count: int = Field(0, description="Number of contacts at this account")
    deal_count: int = Field(0, description="Number of deals involving this account")
    pipeline_value: float = Field(0.0, description="Sum of open deal amounts for this account")
    research_status: str | None = None
    last_researched_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AccountContactItem(BaseModel):
    """A contact belonging to an account."""

    contact_id: UUID
    full_name: str | None = None
    email: str | None = None
    job_title: str | None = None
    deal_count: int = 0


class AccountDealItem(BaseModel):
    """A deal involving this account."""

    deal_id: UUID
    name: str | None = None
    amount: float | None = None
    currency: str | None = None
    deal_status: str | None = None
    stage_name: str | None = None
    owner_name: str | None = None


class AccountDetail(AccountListItem):
    """Extended account detail with contacts and deals."""

    description: str | None = None
    company_type: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    timezone: str | None = None
    company_research: str | None = Field(None, description="Markdown enrichment profile")
    contacts: list[AccountContactItem] = Field(
        default_factory=list, description="Contacts at this account"
    )
    deals: list[AccountDealItem] = Field(
        default_factory=list, description="Deals involving this account"
    )


class AccountsPage(BaseModel):
    """Paginated accounts list."""

    items: list[AccountListItem]
    page: int = Field(..., description="Current page (1-indexed)")
    limit: int = Field(..., description="Page size")
    total_count: int = Field(..., description="Total accounts matching filters")
    total_pages: int


class CreateAccountRequest(BaseModel):
    """Create a new account. Name required, domain optional (auto-links contacts)."""

    name: str = Field(..., description="Company name (required)")
    domain: str | None = Field(None, description="Company domain (optional, used to auto-link contacts)")
    website: str | None = None
    industry: str | None = None
    linkedin_url: str | None = None


class UpdateAccountRequest(BaseModel):
    """Update an existing account. All fields optional."""

    name: str | None = None
    domain: str | None = None
    website: str | None = None
    industry: str | None = None
    description: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    number_of_employees: int | None = None
    annual_revenue: float | None = None


class EnrichAccountResponse(BaseModel):
    """Response after triggering account enrichment."""

    job_id: str | None = Field(None, description="Research job ID (null if skipped)")
    message: str
```

- [ ] **Step 2: Verify the schema file imports cleanly**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run python -c "from src.api.schemas.crm_account_models import AccountsPage, CreateAccountRequest; print('ok')"`

Expected: `ok`

---

## Task 4: ContactsAccessService

**Files:**
- Create: `src/service/crm/access/contacts_access_service.py`
- Test: `tests/service/crm/test_contacts_access_service.py`

- [ ] **Step 1: Write tests for the service**

Create `tests/service/crm/test_contacts_access_service.py`:

```python
"""Tests for ContactsAccessService."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest


# ── Fakes ────────────────────────────────────────────────────────────────

class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def first(self):
        return self._value[0] if isinstance(self._value, list) and self._value else self._value

    def all(self):
        return self._value if isinstance(self._value, list) else []

    def one(self):
        return self._value


class _FakeSession:
    """Minimal async session for unit tests."""

    def __init__(self, results: list | None = None):
        self._results = results or []
        self._call_idx = 0
        self.added: list = []
        self.flushed = False

    async def execute(self, stmt):
        if self._call_idx < len(self._results):
            result = self._results[self._call_idx]
            self._call_idx += 1
            return _FakeScalarResult(result)
        return _FakeScalarResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def refresh(self, obj):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def close(self):
        pass


# ── Tests ────────────────────────────────────────────────────────────────


def test_extract_domain_from_email():
    from src.service.crm.access.contacts_access_service import ContactsAccessService

    service = ContactsAccessService(
        org_session=_FakeSession(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
    )
    assert service._extract_domain("john@acme.com") == "acme.com"
    assert service._extract_domain("JANE@Example.IO") == "example.io"
    assert service._extract_domain("bad-email") is None
    assert service._extract_domain("") is None


def test_split_name():
    from src.service.crm.access.contacts_access_service import ContactsAccessService

    service = ContactsAccessService(
        org_session=_FakeSession(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
    )
    first, last = service._split_name("John Doe")
    assert first == "John"
    assert last == "Doe"

    first, last = service._split_name("Madonna")
    assert first == "Madonna"
    assert last is None

    first, last = service._split_name("Mary Jane Watson")
    assert first == "Mary"
    assert last == "Jane Watson"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run pytest tests/service/crm/test_contacts_access_service.py -v`

Expected: FAIL — `ContactsAccessService` not found.

- [ ] **Step 3: Implement ContactsAccessService**

Create `src/service/crm/access/contacts_access_service.py`:

```python
"""Service for CRM Contacts read and write operations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)

logger = logging.getLogger(__name__)


class ContactsAccessServiceError(Exception):
    """Custom exception for contacts service errors."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class ContactsAccessService:
    """Service for managing CRM contacts."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("ContactsAccessService close exception", exc_info=True)

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_domain(email: str) -> str | None:
        """Extract lowered domain from an email address."""
        if not email or "@" not in email:
            return None
        return email.rsplit("@", 1)[1].lower()

    @staticmethod
    def _split_name(full_name: str) -> tuple[str, str | None]:
        """Split 'First Last' into (first, last). Last may be None."""
        parts = full_name.strip().split(None, 1)
        first = parts[0] if parts else full_name
        last = parts[1] if len(parts) > 1 else None
        return first, last

    async def _get_internal_integration(self) -> KnowledgeIntegration | None:
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == self.organisation_id,
            col(KnowledgeIntegration.integration_type) == IntegrationType.INTERNAL_CRM.value,
        )
        result = await self.org_session.execute(stmt)
        return result.scalars().first()

    async def _ensure_internal_integration(self) -> KnowledgeIntegration:
        existing = await self._get_internal_integration()
        if existing:
            return existing
        integration = KnowledgeIntegration(
            organisation_id=self.organisation_id,
            created_by_user_id=None,
            created_by_admin=False,
            integration_type=IntegrationType.INTERNAL_CRM,
            integration_scope=IntegrationScope.ORGANIZATION,
            name="Copilot CRM",
            credentials={},
            auto_sync_enabled=False,
            sync_frequency="manual",
            connection_metadata={"source": "internal_crm", "internal": True},
            is_active=True,
        )
        self.org_session.add(integration)
        await self.org_session.flush()
        await self.org_session.refresh(integration)
        return integration

    async def _find_company_by_domain(self, domain: str) -> CRMCompany | None:
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            func.lower(col(CRMCompany.domain)) == domain.lower(),
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        result = await self.org_session.execute(stmt)
        return result.scalars().first()

    # ── List ─────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        company_id: UUID | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return paginated contacts with deal counts.

        Returns (items, total_count) where each item is a dict
        with CRMContact fields plus 'deal_count' and 'source'.
        """
        # Base query
        base = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )

        if q:
            pattern = f"%{q}%"
            base = base.where(
                (col(CRMContact.full_name).ilike(pattern))
                | (col(CRMContact.email).ilike(pattern))
                | (col(CRMContact.company_name).ilike(pattern))
            )

        if company_id:
            # Find company domain, match contacts by owner or company_name
            company = await self._find_company_by_id(company_id)
            if company and company.domain:
                base = base.where(
                    col(CRMContact.email).ilike(f"%@{company.domain}")
                )

        # Count
        count_stmt = select(func.count()).select_from(base.subquery())
        total_count = (await self.org_session.execute(count_stmt)).scalar_one()

        # Page
        offset = (page - 1) * limit
        stmt = base.order_by(col(CRMContact.full_name).asc()).offset(offset).limit(limit)
        result = await self.org_session.execute(stmt)
        contacts = result.scalars().all()

        # Annotate with deal counts
        items = []
        for c in contacts:
            deal_count = await self._count_deals_for_contact(c.contact_id)
            source = await self._resolve_source(c.integration_id)
            items.append({
                "contact": c,
                "deal_count": deal_count,
                "source": source,
                "company_id": await self._resolve_company_id(c),
                "company_domain": self._extract_domain(c.email) if c.email else None,
            })

        return items, total_count

    async def _find_company_by_id(self, company_id: UUID) -> CRMCompany | None:
        stmt = select(CRMCompany).where(
            col(CRMCompany.company_id) == company_id,
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        result = await self.org_session.execute(stmt)
        return result.scalars().first()

    async def _count_deals_for_contact(self, contact_id: UUID) -> int:
        stmt = select(func.count()).where(
            col(DealStakeholder.contact_id) == contact_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await self.org_session.execute(stmt)
        return result.scalar_one()

    async def _resolve_source(self, integration_id: UUID) -> str:
        stmt = select(KnowledgeIntegration.integration_type).where(
            col(KnowledgeIntegration.integration_id) == integration_id,
        )
        result = await self.org_session.execute(stmt)
        itype = result.scalar_one_or_none()
        if itype == IntegrationType.INTERNAL_CRM.value:
            return "internal"
        return "crm_sync"

    async def _resolve_company_id(self, contact: CRMContact) -> UUID | None:
        """Try to find the company for a contact by email domain."""
        if not contact.email:
            return None
        domain = self._extract_domain(contact.email)
        if not domain:
            return None
        company = await self._find_company_by_domain(domain)
        return company.company_id if company else None

    # ── Detail ───────────────────────────────────────────────────────

    async def get_contact_detail(self, contact_id: UUID) -> dict[str, Any]:
        """Get full contact with deal involvement."""
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        result = await self.org_session.execute(stmt)
        contact = result.scalars().first()
        if not contact:
            raise ContactsAccessServiceError("Contact not found", status_code=404)

        # Deal involvement via stakeholders
        involvement_stmt = (
            select(DealStakeholder, CRMDeal)
            .join(CRMDeal, col(DealStakeholder.deal_id) == col(CRMDeal.deal_id))
            .where(
                col(DealStakeholder.contact_id) == contact_id,
                col(DealStakeholder.organisation_id) == self.organisation_id,
            )
        )
        inv_result = await self.org_session.execute(involvement_stmt)
        rows = inv_result.all()

        deal_involvement = []
        for stakeholder, deal in rows:
            deal_involvement.append({
                "deal_id": deal.deal_id,
                "deal_name": deal.name,
                "deal_status": deal.deal_status,
                "stage_name": None,  # would need join to stages — keep simple for now
                "stakeholder_id": stakeholder.stakeholder_id,
                "stance": stakeholder.stance.value if stakeholder.stance else None,
                "decision_power": stakeholder.decision_power.value if stakeholder.decision_power else None,
                "relationship_role": None,
            })

        source = await self._resolve_source(contact.integration_id)
        company_id = await self._resolve_company_id(contact)
        domain = self._extract_domain(contact.email) if contact.email else None

        return {
            "contact": contact,
            "deal_count": len(deal_involvement),
            "deal_involvement": deal_involvement,
            "source": source,
            "company_id": company_id,
            "company_domain": domain,
        }

    # ── Create ───────────────────────────────────────────────────────

    async def create_contact(self, email: str, name: str, **kwargs: Any) -> CRMContact:
        """Create a new CRM contact with auto account resolution."""
        # Check duplicate email in org
        dup_stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            func.lower(col(CRMContact.email)) == email.lower(),
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        dup_result = await self.org_session.execute(dup_stmt)
        if dup_result.scalars().first():
            raise ContactsAccessServiceError(
                f"Contact with email {email} already exists", status_code=409
            )

        integration = await self._ensure_internal_integration()
        now = datetime.now(UTC)
        first_name, last_name = self._split_name(name)

        # Auto-resolve company from domain
        domain = self._extract_domain(email)
        company_name = None
        if domain:
            company = await self._find_company_by_domain(domain)
            if company:
                company_name = company.name

        contact_id = uuid4()
        contact = CRMContact(
            contact_id=contact_id,
            external_id=str(contact_id),
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            first_name=first_name,
            last_name=last_name,
            full_name=name,
            email=email.lower(),
            job_title=kwargs.get("job_title"),
            phone=kwargs.get("phone"),
            company_name=company_name,
            additional_emails=[],
            properties={},
            metrics={},
            data_payload={"source": "internal_crm"},
            synced_at=now,
            is_deleted=False,
        )

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.refresh(contact)
        return contact

    # ── Update ───────────────────────────────────────────────────────

    async def update_contact(self, contact_id: UUID, updates: dict[str, Any]) -> CRMContact:
        """Update editable fields on a contact."""
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        result = await self.org_session.execute(stmt)
        contact = result.scalars().first()
        if not contact:
            raise ContactsAccessServiceError("Contact not found", status_code=404)

        allowed = {
            "first_name", "last_name", "full_name", "job_title",
            "phone", "mobile_phone", "company_name",
        }
        for key, value in updates.items():
            if key in allowed and value is not None:
                setattr(contact, key, value)

        # Handle linkedin_url → properties
        if "linkedin_url" in updates and updates["linkedin_url"] is not None:
            props = dict(contact.properties) if contact.properties else {}
            props["linkedin_url"] = updates["linkedin_url"]
            contact.properties = props

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.refresh(contact)
        return contact

    # ── Add to Deal ──────────────────────────────────────────────────

    async def add_contact_to_deal(
        self, contact_id: UUID, deal_id: UUID, relationship_role: str | None = None
    ) -> dict[str, Any]:
        """Add a contact as a stakeholder on a deal.

        Delegates to DealParticipantService internally.
        Returns dict with created participant info.
        """
        from src.service.crm.internal.deal_participant_service import (
            DealParticipantService,
            DealParticipantServiceError,
        )

        # Verify contact exists
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        result = await self.org_session.execute(stmt)
        contact = result.scalars().first()
        if not contact:
            raise ContactsAccessServiceError("Contact not found", status_code=404)

        participant_svc = DealParticipantService(
            session=self.org_session,
            organisation_id=self.organisation_id,
            actor_user_id=uuid4(),  # will be overridden by handler with real user_id
        )

        try:
            created, skipped, _, _ = await participant_svc.add_participants(
                deal_id=deal_id,
                payloads=[{
                    "participant_type": "contact",
                    "related_id": contact_id,
                    "relationship_role": relationship_role,
                }],
            )
        except DealParticipantServiceError as e:
            raise ContactsAccessServiceError(e.message, status_code=e.status_code) from e

        if skipped:
            return {"added": False, "reason": skipped[0].reason}
        return {"added": True, "participant_id": str(created[0].participant_id) if created else None}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run pytest tests/service/crm/test_contacts_access_service.py -v`

Expected: 3 tests PASS.

---

## Task 5: AccountsAccessService

**Files:**
- Create: `src/service/crm/access/accounts_access_service.py`
- Test: `tests/service/crm/test_accounts_access_service.py`

- [ ] **Step 1: Write tests for the service**

Create `tests/service/crm/test_accounts_access_service.py`:

```python
"""Tests for AccountsAccessService."""

from __future__ import annotations

from uuid import uuid4

import pytest


def test_normalize_domain():
    from src.service.crm.access.accounts_access_service import AccountsAccessService

    service = AccountsAccessService(
        org_session=None,  # type: ignore[arg-type]
        organisation_id=uuid4(),
    )
    assert service._normalize_domain("ACME.COM") == "acme.com"
    assert service._normalize_domain("https://www.acme.com") == "acme.com"
    assert service._normalize_domain("http://Acme.com/about") == "acme.com"
    assert service._normalize_domain("") is None
    assert service._normalize_domain(None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run pytest tests/service/crm/test_accounts_access_service.py -v`

Expected: FAIL — `AccountsAccessService` not found.

- [ ] **Step 3: Implement AccountsAccessService**

Create `src/service/crm/access/accounts_access_service.py`:

```python
"""Service for CRM Accounts read and write operations."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)

logger = logging.getLogger(__name__)


class AccountsAccessServiceError(Exception):
    """Custom exception for accounts service errors."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class AccountsAccessService:
    """Service for managing CRM accounts (companies)."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("AccountsAccessService close exception", exc_info=True)

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _normalize_domain(domain: str | None) -> str | None:
        """Normalize a domain: strip protocol, www, path, lowercase."""
        if not domain:
            return None
        d = domain.strip().lower()
        d = re.sub(r"^https?://", "", d)
        d = re.sub(r"^www\.", "", d)
        d = d.split("/")[0]
        return d if d else None

    async def _get_internal_integration(self) -> KnowledgeIntegration | None:
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == self.organisation_id,
            col(KnowledgeIntegration.integration_type) == IntegrationType.INTERNAL_CRM.value,
        )
        result = await self.org_session.execute(stmt)
        return result.scalars().first()

    async def _ensure_internal_integration(self) -> KnowledgeIntegration:
        existing = await self._get_internal_integration()
        if existing:
            return existing
        integration = KnowledgeIntegration(
            organisation_id=self.organisation_id,
            created_by_user_id=None,
            created_by_admin=False,
            integration_type=IntegrationType.INTERNAL_CRM,
            integration_scope=IntegrationScope.ORGANIZATION,
            name="Copilot CRM",
            credentials={},
            auto_sync_enabled=False,
            sync_frequency="manual",
            connection_metadata={"source": "internal_crm", "internal": True},
            is_active=True,
        )
        self.org_session.add(integration)
        await self.org_session.flush()
        await self.org_session.refresh(integration)
        return integration

    # ── List ─────────────────────────────────────────────────────────

    async def list_accounts(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        industry: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return paginated accounts with contact/deal counts and pipeline value."""
        base = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )

        if q:
            pattern = f"%{q}%"
            base = base.where(
                (col(CRMCompany.name).ilike(pattern))
                | (col(CRMCompany.domain).ilike(pattern))
            )

        if industry:
            base = base.where(col(CRMCompany.industry) == industry)

        count_stmt = select(func.count()).select_from(base.subquery())
        total_count = (await self.org_session.execute(count_stmt)).scalar_one()

        offset = (page - 1) * limit
        stmt = base.order_by(col(CRMCompany.name).asc()).offset(offset).limit(limit)
        result = await self.org_session.execute(stmt)
        companies = result.scalars().all()

        items = []
        for company in companies:
            contact_count = await self._count_contacts(company)
            deal_count, pipeline_value = await self._count_deals_and_pipeline(company)
            items.append({
                "company": company,
                "contact_count": contact_count,
                "deal_count": deal_count,
                "pipeline_value": pipeline_value,
            })

        return items, total_count

    async def _count_contacts(self, company: CRMCompany) -> int:
        """Count contacts matching this company's domain."""
        if not company.domain:
            return 0
        stmt = select(func.count()).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.email).ilike(f"%@{company.domain}"),
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        result = await self.org_session.execute(stmt)
        return result.scalar_one()

    async def _count_deals_and_pipeline(self, company: CRMCompany) -> tuple[int, float]:
        """Count deals and sum open pipeline for this company."""
        stmt = select(func.count(), func.coalesce(func.sum(CRMDeal.amount), 0.0)).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.company_external_id) == company.external_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
        )
        result = await self.org_session.execute(stmt)
        row = result.one()
        total_deals = row[0]
        pipeline = float(row[1])
        return total_deals, pipeline

    # ── Detail ───────────────────────────────────────────────────────

    async def get_account_detail(self, company_id: UUID) -> dict[str, Any]:
        """Get full account with contacts and deals."""
        stmt = select(CRMCompany).where(
            col(CRMCompany.company_id) == company_id,
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        result = await self.org_session.execute(stmt)
        company = result.scalars().first()
        if not company:
            raise AccountsAccessServiceError("Account not found", status_code=404)

        contacts = await self.get_account_contacts(company_id)
        deals = await self.get_account_deals(company_id)
        contact_count = len(contacts)
        deal_count = len(deals)
        pipeline_value = sum(d.amount or 0 for d in deals if d.deal_status == "open")

        return {
            "company": company,
            "contacts": contacts,
            "deals": deals,
            "contact_count": contact_count,
            "deal_count": deal_count,
            "pipeline_value": pipeline_value,
        }

    async def get_account_contacts(self, company_id: UUID) -> list[CRMContact]:
        """Get contacts belonging to an account (matched by domain)."""
        company = await self._get_company(company_id)
        if not company or not company.domain:
            return []

        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.email).ilike(f"%@{company.domain}"),
            col(CRMContact.is_deleted) == False,  # noqa: E712
        ).order_by(col(CRMContact.full_name).asc())
        result = await self.org_session.execute(stmt)
        return list(result.scalars().all())

    async def get_account_deals(self, company_id: UUID) -> list[CRMDeal]:
        """Get deals for an account (matched by company_external_id)."""
        company = await self._get_company(company_id)
        if not company:
            return []

        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.company_external_id) == company.external_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
        ).order_by(col(CRMDeal.created_at).desc())
        result = await self.org_session.execute(stmt)
        return list(result.scalars().all())

    async def _get_company(self, company_id: UUID) -> CRMCompany | None:
        stmt = select(CRMCompany).where(
            col(CRMCompany.company_id) == company_id,
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        result = await self.org_session.execute(stmt)
        return result.scalars().first()

    # ── Create ───────────────────────────────────────────────────────

    async def create_account(self, name: str, **kwargs: Any) -> CRMCompany:
        """Create a new CRM company/account."""
        domain = self._normalize_domain(kwargs.get("domain"))

        # Check duplicate by domain if provided
        if domain:
            existing = await self._find_company_by_domain(domain)
            if existing:
                raise AccountsAccessServiceError(
                    f"Account with domain {domain} already exists", status_code=409
                )

        integration = await self._ensure_internal_integration()
        now = datetime.now(UTC)
        company_id = uuid4()

        company = CRMCompany(
            company_id=company_id,
            external_id=str(company_id),
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            name=name,
            domain=domain,
            website=kwargs.get("website"),
            industry=kwargs.get("industry"),
            linkedin_url=kwargs.get("linkedin_url"),
            description=None,
            company_type=None,
            lifecycle_stage=None,
            number_of_employees=None,
            annual_revenue=None,
            phone=None,
            is_active=True,
            properties={},
            metrics={},
            data_payload={"source": "internal_crm"},
            synced_at=now,
            is_deleted=False,
        )

        self.org_session.add(company)
        await self.org_session.flush()
        await self.org_session.refresh(company)
        return company

    async def _find_company_by_domain(self, domain: str) -> CRMCompany | None:
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            func.lower(col(CRMCompany.domain)) == domain.lower(),
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        result = await self.org_session.execute(stmt)
        return result.scalars().first()

    # ── Update ───────────────────────────────────────────────────────

    async def update_account(self, company_id: UUID, updates: dict[str, Any]) -> CRMCompany:
        """Update editable fields on an account."""
        company = await self._get_company(company_id)
        if not company:
            raise AccountsAccessServiceError("Account not found", status_code=404)

        allowed = {
            "name", "domain", "website", "industry", "description",
            "phone", "linkedin_url", "number_of_employees", "annual_revenue",
        }
        for key, value in updates.items():
            if key in allowed and value is not None:
                if key == "domain":
                    value = self._normalize_domain(value)
                setattr(company, key, value)

        self.org_session.add(company)
        await self.org_session.flush()
        await self.org_session.refresh(company)
        return company
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run pytest tests/service/crm/test_accounts_access_service.py -v`

Expected: 1 test PASS.

---

## Task 6: Contacts API Handler

**Files:**
- Create: `src/api/routers/crm/contacts_handler.py`
- Modify: `src/api/routers/crm/__init__.py`
- Modify: `src/api/fastapi_app.py`

- [ ] **Step 1: Create the contacts handler**

Create `src/api/routers/crm/contacts_handler.py`:

```python
"""CRM Contacts API handler — tenant-scoped endpoints for contact management."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_contact_models import (
    AddContactToDealRequest,
    ContactDetail,
    ContactDealInvolvement,
    ContactListItem,
    ContactsPage,
    CreateContactRequest,
    EnrichContactResponse,
    UpdateContactRequest,
)
from src.api.utils.http_response import Response
from src.service.crm.access.contacts_access_service import (
    ContactsAccessService,
    ContactsAccessServiceError,
)

logger = logging.getLogger(__name__)

contacts_router = APIRouter()

_UNEXPECTED_ERR = "An unexpected error occurred"


# ── Dependencies ─────────────────────────────────────────────────────

async def get_contacts_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    service = ContactsAccessService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Contacts service close failed", exc_info=True)


# ── Endpoints ────────────────────────────────────────────────────────

@contacts_router.get(
    "/contacts",
    response_model=GenericResponse[ContactsPage],
    tags=["crm", "contacts"],
    summary="List CRM contacts with pagination and filtering",
)
async def list_contacts(
    response: FastAPIResponse,
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    q: str | None = Query(None, description="Search by name, email, or company"),
    company_id: UUID | None = Query(None, description="Filter by account/company ID"),
    service: ContactsAccessService = Depends(get_contacts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_read),
) -> GenericResponse[ContactsPage]:
    try:
        items, total_count = await service.list_contacts(
            page=page, limit=limit, q=q, company_id=company_id,
        )
        total_pages = (total_count + limit - 1) // limit if limit else 0

        contact_items = [
            ContactListItem(
                contact_id=row["contact"]["contact_id"] if isinstance(row["contact"], dict) else row["contact"].contact_id,
                external_id=row["contact"].external_id,
                first_name=row["contact"].first_name,
                last_name=row["contact"].last_name,
                full_name=row["contact"].full_name,
                email=row["contact"].email,
                job_title=row["contact"].job_title,
                phone=row["contact"].phone,
                company_name=row["contact"].company_name,
                company_id=row["company_id"],
                company_domain=row["company_domain"],
                lifecycle_stage=row["contact"].lifecycle_stage,
                owner_name=row["contact"].owner_name,
                deal_count=row["deal_count"],
                source=row["source"],
                research_status=row["contact"].research_status,
                last_researched_at=row["contact"].last_researched_at,
                created_at=row["contact"].created_at,
                updated_at=row["contact"].updated_at,
            )
            for row in items
        ]

        page_response = ContactsPage(
            items=contact_items,
            page=page,
            limit=limit,
            total_count=total_count,
            total_pages=total_pages,
        )
        return Response.success_response(message="Contacts retrieved", data=page_response)

    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error listing contacts")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@contacts_router.post(
    "/contacts",
    response_model=GenericResponse[ContactListItem],
    tags=["crm", "contacts"],
    summary="Create a new CRM contact",
)
async def create_contact(
    body: CreateContactRequest,
    response: FastAPIResponse,
    service: ContactsAccessService = Depends(get_contacts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[ContactListItem]:
    try:
        contact = await service.create_contact(
            email=body.email,
            name=body.name,
            job_title=body.job_title,
            phone=body.phone,
        )
        item = ContactListItem(
            contact_id=contact.contact_id,
            external_id=contact.external_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            full_name=contact.full_name,
            email=contact.email,
            job_title=contact.job_title,
            phone=contact.phone,
            company_name=contact.company_name,
            company_id=None,
            company_domain=ContactsAccessService._extract_domain(contact.email) if contact.email else None,
            deal_count=0,
            source="internal",
            research_status=None,
            last_researched_at=None,
            created_at=contact.created_at,
            updated_at=contact.updated_at,
        )
        return Response.created_response(message="Contact created", data=item, response=response)

    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error creating contact")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@contacts_router.get(
    "/contacts/{contact_id}",
    response_model=GenericResponse[ContactDetail],
    tags=["crm", "contacts"],
    summary="Get contact detail with deal involvement",
)
async def get_contact_detail(
    contact_id: UUID,
    response: FastAPIResponse,
    service: ContactsAccessService = Depends(get_contacts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_read),
) -> GenericResponse[ContactDetail]:
    try:
        data = await service.get_contact_detail(contact_id)
        c = data["contact"]
        detail = ContactDetail(
            contact_id=c.contact_id,
            external_id=c.external_id,
            first_name=c.first_name,
            last_name=c.last_name,
            full_name=c.full_name,
            email=c.email,
            job_title=c.job_title,
            phone=c.phone,
            company_name=c.company_name,
            company_id=data["company_id"],
            company_domain=data["company_domain"],
            deal_count=data["deal_count"],
            source=data["source"],
            research_status=c.research_status,
            last_researched_at=c.last_researched_at,
            created_at=c.created_at,
            updated_at=c.updated_at,
            additional_emails=c.additional_emails or [],
            mobile_phone=c.mobile_phone,
            linkedin_url=c.properties.get("linkedin_url") if c.properties else None,
            address=c.address,
            city=c.city,
            state=c.state,
            country=c.country,
            contact_research=c.contact_research,
            last_contacted_at=c.last_contacted_at,
            last_activity_at=c.last_activity_at,
            deal_involvement=[
                ContactDealInvolvement(**inv) for inv in data["deal_involvement"]
            ],
        )
        return Response.success_response(message="Contact detail retrieved", data=detail)

    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error getting contact detail")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@contacts_router.patch(
    "/contacts/{contact_id}",
    response_model=GenericResponse[ContactListItem],
    tags=["crm", "contacts"],
    summary="Update a CRM contact",
)
async def update_contact(
    contact_id: UUID,
    body: UpdateContactRequest,
    response: FastAPIResponse,
    service: ContactsAccessService = Depends(get_contacts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[ContactListItem]:
    try:
        updates = body.model_dump(exclude_unset=True)
        contact = await service.update_contact(contact_id, updates)
        item = ContactListItem(
            contact_id=contact.contact_id,
            external_id=contact.external_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            full_name=contact.full_name,
            email=contact.email,
            job_title=contact.job_title,
            phone=contact.phone,
            company_name=contact.company_name,
            company_id=None,
            company_domain=ContactsAccessService._extract_domain(contact.email) if contact.email else None,
            deal_count=0,
            source="internal",
            research_status=contact.research_status,
            last_researched_at=contact.last_researched_at,
            created_at=contact.created_at,
            updated_at=contact.updated_at,
        )
        return Response.success_response(message="Contact updated", data=item)

    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error updating contact")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@contacts_router.post(
    "/contacts/{contact_id}/enrich",
    response_model=GenericResponse[EnrichContactResponse],
    tags=["crm", "contacts"],
    summary="Trigger LinkedIn enrichment for a contact",
)
async def enrich_contact(
    contact_id: UUID,
    response: FastAPIResponse,
    service: ContactsAccessService = Depends(get_contacts_service),
    user_ids: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[EnrichContactResponse]:
    try:
        # Verify contact exists
        data = await service.get_contact_detail(contact_id)
        contact = data["contact"]

        # Dispatch research — reuse stakeholder research for now
        # If contact is linked to a stakeholder, use that; otherwise create placeholder
        from src.workers.deal_worker.utils.stakeholder_research_dispatch import (
            dispatch_stakeholder_research_async,
        )

        # Find any linked stakeholder to dispatch research through
        from sqlalchemy import select
        from sqlmodel import col
        from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder

        stmt = select(DealStakeholder).where(
            col(DealStakeholder.contact_id) == contact_id,
            col(DealStakeholder.organisation_id) == user_ids.organisation_id,
        ).limit(1)
        result = await service.org_session.execute(stmt)
        stakeholder = result.scalars().first()

        if stakeholder:
            job_id = await dispatch_stakeholder_research_async(
                organisation_id=user_ids.organisation_id,
                stakeholder_id=stakeholder.stakeholder_id,
                deal_id=stakeholder.deal_id,
                user_id=user_ids.user_id,
                force=True,
            )
            resp = EnrichContactResponse(
                job_id=job_id,
                message="Enrichment dispatched" if job_id else "Enrichment skipped (cooldown)",
            )
        else:
            resp = EnrichContactResponse(
                job_id=None,
                message="Contact must be added to a deal before enrichment can run",
            )

        return Response.success_response(message=resp.message, data=resp)

    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error enriching contact")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@contacts_router.post(
    "/contacts/{contact_id}/add-to-deal",
    response_model=GenericResponse[dict],
    tags=["crm", "contacts"],
    summary="Add a contact as a stakeholder on a deal",
)
async def add_contact_to_deal(
    contact_id: UUID,
    body: AddContactToDealRequest,
    response: FastAPIResponse,
    service: ContactsAccessService = Depends(get_contacts_service),
    user_ids: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[dict]:
    try:
        result = await service.add_contact_to_deal(
            contact_id=contact_id,
            deal_id=body.deal_id,
            relationship_role=body.relationship_role,
        )
        msg = "Contact added to deal" if result.get("added") else result.get("reason", "Skipped")
        return Response.success_response(message=msg, data=result, response=response)

    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error adding contact to deal")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)
```

- [ ] **Step 2: Register the router**

In `src/api/routers/crm/__init__.py`, add:

```python
from .contacts_handler import contacts_router
```

And add `"contacts_router"` to the `__all__` list.

In `src/api/fastapi_app.py`, add after the existing CRM router registrations:

```python
    fastapp.include_router(contacts_router, prefix="/users/crm", tags=["crm-contacts"])
```

Also add the import at the top of fastapi_app.py where other CRM routers are imported:

```python
from src.api.routers.crm import contacts_router
```

- [ ] **Step 3: Verify the handler imports cleanly**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run python -c "from src.api.routers.crm.contacts_handler import contacts_router; print('ok')"`

Expected: `ok`

---

## Task 7: Accounts API Handler

**Files:**
- Create: `src/api/routers/crm/accounts_handler.py`
- Modify: `src/api/routers/crm/__init__.py`
- Modify: `src/api/fastapi_app.py`

- [ ] **Step 1: Create the accounts handler**

Create `src/api/routers/crm/accounts_handler.py`:

```python
"""CRM Accounts API handler — tenant-scoped endpoints for account management."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_account_models import (
    AccountContactItem,
    AccountDealItem,
    AccountDetail,
    AccountListItem,
    AccountsPage,
    CreateAccountRequest,
    EnrichAccountResponse,
    UpdateAccountRequest,
)
from src.api.utils.http_response import Response
from src.service.crm.access.accounts_access_service import (
    AccountsAccessService,
    AccountsAccessServiceError,
)

logger = logging.getLogger(__name__)

accounts_router = APIRouter()

_UNEXPECTED_ERR = "An unexpected error occurred"


# ── Dependencies ─────────────────────────────────────────────────────

async def get_accounts_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    service = AccountsAccessService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Accounts service close failed", exc_info=True)


# ── Endpoints ────────────────────────────────────────────────────────

@accounts_router.get(
    "/accounts",
    response_model=GenericResponse[AccountsPage],
    tags=["crm", "accounts"],
    summary="List CRM accounts with pagination and filtering",
)
async def list_accounts(
    response: FastAPIResponse,
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    q: str | None = Query(None, description="Search by name or domain"),
    industry: str | None = Query(None, description="Filter by industry"),
    service: AccountsAccessService = Depends(get_accounts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_read),
) -> GenericResponse[AccountsPage]:
    try:
        items, total_count = await service.list_accounts(
            page=page, limit=limit, q=q, industry=industry,
        )
        total_pages = (total_count + limit - 1) // limit if limit else 0

        account_items = [
            AccountListItem(
                company_id=row["company"].company_id,
                external_id=row["company"].external_id,
                name=row["company"].name,
                domain=row["company"].domain,
                website=row["company"].website,
                industry=row["company"].industry,
                lifecycle_stage=row["company"].lifecycle_stage,
                owner_name=row["company"].owner_name,
                number_of_employees=row["company"].number_of_employees,
                annual_revenue=row["company"].annual_revenue,
                phone=row["company"].phone,
                country=row["company"].country,
                linkedin_url=row["company"].linkedin_url,
                is_active=row["company"].is_active,
                contact_count=row["contact_count"],
                deal_count=row["deal_count"],
                pipeline_value=row["pipeline_value"],
                research_status=row["company"].research_status,
                last_researched_at=row["company"].last_researched_at,
                created_at=row["company"].created_at,
                updated_at=row["company"].updated_at,
            )
            for row in items
        ]

        page_response = AccountsPage(
            items=account_items,
            page=page,
            limit=limit,
            total_count=total_count,
            total_pages=total_pages,
        )
        return Response.success_response(message="Accounts retrieved", data=page_response)

    except AccountsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error listing accounts")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@accounts_router.post(
    "/accounts",
    response_model=GenericResponse[AccountListItem],
    tags=["crm", "accounts"],
    summary="Create a new CRM account",
)
async def create_account(
    body: CreateAccountRequest,
    response: FastAPIResponse,
    service: AccountsAccessService = Depends(get_accounts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[AccountListItem]:
    try:
        company = await service.create_account(
            name=body.name,
            domain=body.domain,
            website=body.website,
            industry=body.industry,
            linkedin_url=body.linkedin_url,
        )
        item = AccountListItem(
            company_id=company.company_id,
            external_id=company.external_id,
            name=company.name,
            domain=company.domain,
            website=company.website,
            industry=company.industry,
            lifecycle_stage=company.lifecycle_stage,
            owner_name=company.owner_name,
            number_of_employees=company.number_of_employees,
            annual_revenue=company.annual_revenue,
            phone=company.phone,
            country=company.country,
            linkedin_url=company.linkedin_url,
            is_active=company.is_active,
            contact_count=0,
            deal_count=0,
            pipeline_value=0.0,
            research_status=None,
            last_researched_at=None,
            created_at=company.created_at,
            updated_at=company.updated_at,
        )
        return Response.created_response(message="Account created", data=item, response=response)

    except AccountsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error creating account")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@accounts_router.get(
    "/accounts/{company_id}",
    response_model=GenericResponse[AccountDetail],
    tags=["crm", "accounts"],
    summary="Get account detail with contacts and deals",
)
async def get_account_detail(
    company_id: UUID,
    response: FastAPIResponse,
    service: AccountsAccessService = Depends(get_accounts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_read),
) -> GenericResponse[AccountDetail]:
    try:
        from sqlalchemy import select, func
        from sqlmodel import col
        from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder

        data = await service.get_account_detail(company_id)
        co = data["company"]

        # Build contact items with deal counts
        contact_items = []
        for c in data["contacts"]:
            dc_stmt = select(func.count()).where(
                col(DealStakeholder.contact_id) == c.contact_id,
                col(DealStakeholder.organisation_id) == service.organisation_id,
            )
            dc_result = await service.org_session.execute(dc_stmt)
            dc = dc_result.scalar_one()
            contact_items.append(AccountContactItem(
                contact_id=c.contact_id,
                full_name=c.full_name,
                email=c.email,
                job_title=c.job_title,
                deal_count=dc,
            ))

        deal_items = [
            AccountDealItem(
                deal_id=d.deal_id,
                name=d.name,
                amount=d.amount,
                currency=d.currency,
                deal_status=d.deal_status,
                stage_name=None,
                owner_name=None,
            )
            for d in data["deals"]
        ]

        detail = AccountDetail(
            company_id=co.company_id,
            external_id=co.external_id,
            name=co.name,
            domain=co.domain,
            website=co.website,
            industry=co.industry,
            lifecycle_stage=co.lifecycle_stage,
            owner_name=co.owner_name,
            number_of_employees=co.number_of_employees,
            annual_revenue=co.annual_revenue,
            phone=co.phone,
            country=co.country,
            linkedin_url=co.linkedin_url,
            is_active=co.is_active,
            contact_count=data["contact_count"],
            deal_count=data["deal_count"],
            pipeline_value=data["pipeline_value"],
            research_status=co.research_status,
            last_researched_at=co.last_researched_at,
            created_at=co.created_at,
            updated_at=co.updated_at,
            description=co.description,
            company_type=co.company_type,
            address=co.address,
            city=co.city,
            state=co.state,
            postal_code=co.postal_code,
            timezone=co.timezone,
            company_research=co.company_research,
            contacts=contact_items,
            deals=deal_items,
        )
        return Response.success_response(message="Account detail retrieved", data=detail)

    except AccountsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error getting account detail")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@accounts_router.patch(
    "/accounts/{company_id}",
    response_model=GenericResponse[AccountListItem],
    tags=["crm", "accounts"],
    summary="Update a CRM account",
)
async def update_account(
    company_id: UUID,
    body: UpdateAccountRequest,
    response: FastAPIResponse,
    service: AccountsAccessService = Depends(get_accounts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[AccountListItem]:
    try:
        updates = body.model_dump(exclude_unset=True)
        company = await service.update_account(company_id, updates)
        item = AccountListItem(
            company_id=company.company_id,
            external_id=company.external_id,
            name=company.name,
            domain=company.domain,
            website=company.website,
            industry=company.industry,
            lifecycle_stage=company.lifecycle_stage,
            owner_name=company.owner_name,
            number_of_employees=company.number_of_employees,
            annual_revenue=company.annual_revenue,
            phone=company.phone,
            country=company.country,
            linkedin_url=company.linkedin_url,
            is_active=company.is_active,
            contact_count=0,
            deal_count=0,
            pipeline_value=0.0,
            research_status=company.research_status,
            last_researched_at=company.last_researched_at,
            created_at=company.created_at,
            updated_at=company.updated_at,
        )
        return Response.success_response(message="Account updated", data=item)

    except AccountsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error updating account")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@accounts_router.get(
    "/accounts/{company_id}/contacts",
    response_model=GenericResponse[list[AccountContactItem]],
    tags=["crm", "accounts"],
    summary="List contacts belonging to an account",
)
async def get_account_contacts(
    company_id: UUID,
    response: FastAPIResponse,
    service: AccountsAccessService = Depends(get_accounts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[AccountContactItem]]:
    try:
        contacts = await service.get_account_contacts(company_id)
        items = [
            AccountContactItem(
                contact_id=c.contact_id,
                full_name=c.full_name,
                email=c.email,
                job_title=c.job_title,
                deal_count=0,
            )
            for c in contacts
        ]
        return Response.success_response(message="Account contacts retrieved", data=items)

    except AccountsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error getting account contacts")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@accounts_router.get(
    "/accounts/{company_id}/deals",
    response_model=GenericResponse[list[AccountDealItem]],
    tags=["crm", "accounts"],
    summary="List deals involving an account",
)
async def get_account_deals(
    company_id: UUID,
    response: FastAPIResponse,
    service: AccountsAccessService = Depends(get_accounts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[AccountDealItem]]:
    try:
        deals = await service.get_account_deals(company_id)
        items = [
            AccountDealItem(
                deal_id=d.deal_id,
                name=d.name,
                amount=d.amount,
                currency=d.currency,
                deal_status=d.deal_status,
                stage_name=None,
                owner_name=None,
            )
            for d in deals
        ]
        return Response.success_response(message="Account deals retrieved", data=items)

    except AccountsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error getting account deals")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)


@accounts_router.post(
    "/accounts/{company_id}/enrich",
    response_model=GenericResponse[EnrichAccountResponse],
    tags=["crm", "accounts"],
    summary="Trigger enrichment for an account",
)
async def enrich_account(
    company_id: UUID,
    response: FastAPIResponse,
    service: AccountsAccessService = Depends(get_accounts_service),
    _user: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[EnrichAccountResponse]:
    try:
        # Verify account exists
        company = await service._get_company(company_id)
        if not company:
            raise AccountsAccessServiceError("Account not found", status_code=404)

        # Account enrichment is deferred — endpoint exists for future implementation
        resp = EnrichAccountResponse(
            job_id=None,
            message="Account enrichment will be available in a future release",
        )
        return Response.success_response(message=resp.message, data=resp)

    except AccountsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response,
        )
    except Exception:
        logger.exception("Unexpected error enriching account")
        return Response.error_response(message=_UNEXPECTED_ERR, status_code=500, response=response)
```

- [ ] **Step 2: Register the router**

In `src/api/routers/crm/__init__.py`, add:

```python
from .accounts_handler import accounts_router
```

And add `"accounts_router"` to the `__all__` list.

In `src/api/fastapi_app.py`, add after the contacts router registration:

```python
    fastapp.include_router(accounts_router, prefix="/users/crm", tags=["crm-accounts"])
```

Also add the import:

```python
from src.api.routers.crm import accounts_router
```

- [ ] **Step 3: Verify the handler imports cleanly**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run python -c "from src.api.routers.crm.accounts_handler import accounts_router; print('ok')"`

Expected: `ok`

---

## Task 8: Lint and Type Check

- [ ] **Step 1: Run ruff check and fix**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run ruff check src/api/schemas/crm_contact_models.py src/api/schemas/crm_account_models.py src/service/crm/access/contacts_access_service.py src/service/crm/access/accounts_access_service.py src/api/routers/crm/contacts_handler.py src/api/routers/crm/accounts_handler.py --fix`

Fix any issues reported.

- [ ] **Step 2: Run mypy on new files**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run mypy src/api/schemas/crm_contact_models.py src/api/schemas/crm_account_models.py src/service/crm/access/contacts_access_service.py src/service/crm/access/accounts_access_service.py`

Fix any type errors.

- [ ] **Step 3: Run all tests**

Run: `cd D:/CopilotGTM/copilot-backend-crm-contacts-accounts && uv run pytest tests/service/crm/test_contacts_access_service.py tests/service/crm/test_accounts_access_service.py -v`

Expected: All tests PASS.

---

## Task Summary

| Task | What | Files |
|------|------|-------|
| 1 | DB Migration — research fields | 1 new migration + 2 model changes |
| 2 | Contacts Pydantic schemas | 1 new schema file |
| 3 | Accounts Pydantic schemas | 1 new schema file |
| 4 | ContactsAccessService | 1 new service + 1 test file |
| 5 | AccountsAccessService | 1 new service + 1 test file |
| 6 | Contacts API handler + registration | 1 new handler + 2 modified files |
| 7 | Accounts API handler + registration | 1 new handler + 2 modified files |
| 8 | Lint and type check | Fix any issues across all new files |
