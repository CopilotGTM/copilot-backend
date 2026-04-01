# Stakeholder-Contact Linking & Domain Matching Fix

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a stakeholder is manually added, ensure a CRMContact exists and link it; fix calendar/email matching to collect customer domains from DealStakeholder records; backfill existing orphaned stakeholders.

**Architecture:** Extract a reusable `_ensure_contact_for_stakeholder()` method on `StakeholdersService` that finds or creates a `CRMContact` by email. For internal CRM deals, also create a `CRMDealParticipant`. Fix both matchers' domain collection to include approved stakeholders. A maintenance script backfills existing stakeholders with `contact_id=NULL`.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async, PostgreSQL, Celery

**Spec:** `docs/superpowers/specs/2026-03-31-stakeholder-contact-linking-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/domain/models/organisation_tables/sync/crm/stakeholders.py` | Modify | Add `created_by_user_id` column to `DealStakeholder` model |
| `migrations/org_migrations/versions/3c7f79637759_041_add_created_by_to_stakeholders.py` | Create | Alembic migration for `created_by_user_id` column |
| `src/service/crm/insights/stakeholders_service.py` | Modify | Add `user_id` param, `_ensure_contact_for_stakeholder()`, `_ensure_deal_participant()`, set `created_by_user_id`, update `create_stakeholder()` and `remove_stakeholder()` |
| `src/api/routers/crm/stakeholders_handler.py` | Modify | Pass `user_ids.user_id` to `StakeholdersService` |
| `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py` | Modify | Make `_collect_customer_domains()` async, add stakeholder domain query |
| `src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py` | Modify | Make `_collect_company_domains()` async, add stakeholder domain query |
| `src/agents/tools/crm/create_stakeholder_if_missing.py` | Modify | Link `contact_id` to existing CRMContact if found |
| `scripts/maintenance/backfill_stakeholder_contacts.py` | Create | One-time backfill for orphaned stakeholders |
| `tests/service/crm/test_stakeholder_contact_linking.py` | Create | Tests for contact creation, participant creation, removal cleanup |
| `tests/workers/deal_worker/services/test_customer_domain_collection.py` | Create | Tests for domain collection from stakeholders |

---

### Task 1: Pass user_id through to StakeholdersService

**Why:** Creating a CRMContact under the internal CRM integration requires `InternalCRMSetupService`, which needs a `user_id` to track who initiated the action. Currently `StakeholdersService` doesn't carry `user_id` — without it, we can't call the internal CRM setup service in later tasks. This is a prerequisite wiring change.

**Files:**
- Modify: `src/service/crm/insights/stakeholders_service.py:46-51`
- Modify: `src/api/routers/crm/stakeholders_handler.py:44-49`
- Modify: `src/service/bots/form_handlers/add_stakeholder_handler.py` (if it constructs StakeholdersService)
- Test: `tests/service/crm/test_stakeholder_contact_linking.py`

- [ ] **Step 1: Add `user_id` parameter to `StakeholdersService.__init__`**

In `src/service/crm/insights/stakeholders_service.py`, update the constructor:

```python
class StakeholdersService:
    """Service for managing CRM deal stakeholders."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID | None = None,
    ):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id
```

- [ ] **Step 2: Pass `user_id` from handler dependency**

In `src/api/routers/crm/stakeholders_handler.py`, update `get_stakeholders_service`:

```python
async def get_stakeholders_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a StakeholdersService instance for the current org."""
    service = StakeholdersService(session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("StakeholdersService close exception", exc_info=True)
```

- [ ] **Step 3: Check `add_stakeholder_handler.py` for StakeholdersService construction**

Read `src/service/bots/form_handlers/add_stakeholder_handler.py` and update any `StakeholdersService(...)` calls to pass `user_id` if available. If user_id is not available in that context, pass `None` (contact creation will use `AUTOMATED_PIPELINE_USER_ID` as fallback).

- [ ] **Step 4: Run existing tests to verify no regressions**

Run: `uv run pytest tests/service/crm/test_stakeholders_service_internal_guard.py -v`
Expected: All existing tests PASS (user_id defaults to None, no behavioral change yet)

- [ ] **Step 5: Commit**

```bash
git add src/service/crm/insights/stakeholders_service.py src/api/routers/crm/stakeholders_handler.py
git commit -m "refactor: add user_id parameter to StakeholdersService"
```

---

### Task 1B: Add `created_by_user_id` column to DealStakeholder

**Why:** When a stakeholder is added manually, we need to track who added it. This is essential for accountability — knowing which user introduced a stakeholder to a deal. For automated sources (CRM sync, calendar/email discovery), this will be `AUTOMATED_PIPELINE_USER_ID`. The `created_at` timestamp from `TimestampMixin` already records when; this column records who.

**Files:**
- Modify: `src/domain/models/organisation_tables/sync/crm/stakeholders.py`
- Create: `migrations/org_migrations/versions/3c7f79637759_041_add_created_by_to_stakeholders.py`

- [ ] **Step 1: Add `created_by_user_id` column to the model**

In `src/domain/models/organisation_tables/sync/crm/stakeholders.py`, add the column in the "Links" section (after `contact_id`):

```python
    # Links
    deal_id: UUID = Field(foreign_key="crm_deals.deal_id", index=True)
    contact_id: UUID | None = Field(default=None, foreign_key="crm_contacts.contact_id", index=True)
    created_by_user_id: UUID | None = Field(
        default=None,
        index=True,
        description="User who added this stakeholder. NULL for legacy records. AUTOMATED_PIPELINE_USER_ID for automated sources.",
    )
```

- [ ] **Step 2: Create the Alembic migration**

Create `migrations/org_migrations/versions/3c7f79637759_041_add_created_by_to_stakeholders.py`:

```python
"""041: Add created_by_user_id to deal_stakeholders.

Revision ID: 3c7f79637759
Revises: 3c7f79637760
Create Date: 2026-03-31
"""

from alembic import op
import sqlalchemy as sa

revision = "3c7f79637759"
down_revision = "3c7f79637760"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deal_stakeholders",
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_deal_stakeholder_created_by",
        "deal_stakeholders",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_deal_stakeholder_created_by", table_name="deal_stakeholders")
    op.drop_column("deal_stakeholders", "created_by_user_id")
```

- [ ] **Step 3: Run the migration locally**

```bash
uv run python -m scripts.run_org_migrations
```

Expected: Migration applies successfully.

- [ ] **Step 4: Commit**

```bash
git add src/domain/models/organisation_tables/sync/crm/stakeholders.py migrations/org_migrations/versions/3c7f79637759_041_add_created_by_to_stakeholders.py
git commit -m "feat: add created_by_user_id column to deal_stakeholders"
```

---

### Task 2: Add `_ensure_contact_for_stakeholder()` method

**Why:** When a user manually adds a stakeholder, the person has no identity in the CRM system — they exist only in `DealStakeholder` with `contact_id=NULL`. This means no cross-deal visibility, no company linkage, and the matching pipelines can't see their domain. This method finds an existing `CRMContact` by email or creates one under the internal CRM integration, giving the stakeholder a proper identity in the contacts table.

**Files:**
- Modify: `src/service/crm/insights/stakeholders_service.py`
- Test: `tests/service/crm/test_stakeholder_contact_linking.py`

- [ ] **Step 1: Write the failing test for contact lookup (existing contact)**

Create `tests/service/crm/test_stakeholder_contact_linking.py`:

```python
"""Tests for stakeholder-to-contact linking in StakeholdersService."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.insights.stakeholders_service import StakeholdersService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def first(self):
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value

    def all(self):
        return self._value if isinstance(self._value, list) else []


class FakeSession:
    """Session that returns canned results based on query content."""

    def __init__(
        self,
        org_domains: list[str] | None = None,
        existing_contact: CRMContact | None = None,
        existing_stakeholder=None,
        internal_integration=None,
    ):
        self.org_domains = org_domains
        self.existing_contact = existing_contact
        self.existing_stakeholder = existing_stakeholder
        self.internal_integration = internal_integration
        self.added: list = []
        self.flushed = False
        self.committed = False

    async def execute(self, stmt):
        compiled = str(stmt).lower()
        if "organisations" in compiled or "domain" in compiled:
            return FakeScalarResult(self.org_domains)
        if "crm_contacts" in compiled:
            return FakeScalarResult(self.existing_contact)
        if "deal_stakeholders" in compiled:
            return FakeScalarResult(self.existing_stakeholder)
        if "knowledge_integrations" in compiled:
            return FakeScalarResult(self.internal_integration)
        return FakeScalarResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        pass

    async def rollback(self):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

ORG_ID = uuid4()
USER_ID = uuid4()
DEAL_ID = uuid4()
INTEGRATION_ID = uuid4()


@pytest.mark.asyncio
async def test_ensure_contact_links_existing_contact() -> None:
    """When a CRMContact exists for the email, return its contact_id."""
    existing_contact = SimpleNamespace(contact_id=uuid4(), email="buyer@customer.com")
    session = FakeSession(existing_contact=existing_contact)
    service = StakeholdersService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    contact_id = await service._ensure_contact_for_stakeholder(
        email="buyer@customer.com",
        name="Buyer Name",
        job_title="VP Sales",
        phone_number=None,
    )

    assert contact_id == existing_contact.contact_id
    # Should not create a new contact
    assert not any(isinstance(obj, CRMContact) for obj in session.added)


@pytest.mark.asyncio
async def test_ensure_contact_creates_new_contact_when_missing() -> None:
    """When no CRMContact exists, create one under the internal integration."""
    internal_integration = SimpleNamespace(integration_id=INTEGRATION_ID)
    session = FakeSession(
        existing_contact=None,
        internal_integration=internal_integration,
    )
    service = StakeholdersService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    contact_id = await service._ensure_contact_for_stakeholder(
        email="new@customer.com",
        name="New Person",
        job_title="Engineer",
        phone_number="+1234567890",
    )

    assert contact_id is not None
    # Should have added a CRMContact
    contacts = [obj for obj in session.added if isinstance(obj, CRMContact)]
    assert len(contacts) == 1
    contact = contacts[0]
    assert contact.email == "new@customer.com"
    assert contact.full_name == "New Person"
    assert contact.job_title == "Engineer"
    assert contact.phone == "+1234567890"
    assert contact.integration_id == INTEGRATION_ID


@pytest.mark.asyncio
async def test_ensure_contact_returns_none_when_no_internal_integration() -> None:
    """When internal CRM integration doesn't exist, return None (no contact created)."""
    session = FakeSession(existing_contact=None, internal_integration=None)
    service = StakeholdersService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    contact_id = await service._ensure_contact_for_stakeholder(
        email="nobody@nowhere.com",
        name="Nobody",
        job_title=None,
        phone_number=None,
    )

    assert contact_id is None
    assert not any(isinstance(obj, CRMContact) for obj in session.added)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/service/crm/test_stakeholder_contact_linking.py -v`
Expected: FAIL — `StakeholdersService` has no method `_ensure_contact_for_stakeholder`

- [ ] **Step 3: Implement `_ensure_contact_for_stakeholder()`**

Add the following imports at the top of `src/service/crm/insights/stakeholders_service.py`:

```python
import json
from uuid import NAMESPACE_URL, uuid5
```

And add to existing imports:

```python
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
```

Add the `AUTOMATED_PIPELINE_USER_ID` constant (or import it if already available):

```python
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
```

Add the method to `StakeholdersService`:

```python
async def _ensure_contact_for_stakeholder(
    self,
    email: str,
    name: str | None,
    job_title: str | None,
    phone_number: str | None,
) -> UUID | None:
    """Find or create a CRMContact for a manually-added stakeholder.

    Returns the contact_id if found or created, None if no internal
    CRM integration exists (contact creation skipped).
    """
    email_normalized = email.lower().strip()

    # Check if a CRMContact already exists by email in this org
    stmt = select(CRMContact).where(
        col(CRMContact.organisation_id) == self.organisation_id,
        col(CRMContact.email) == email_normalized,
    )
    result = await self.session.execute(stmt)
    existing_contact = result.scalars().first()
    if existing_contact:
        return existing_contact.contact_id

    # No existing contact — create one under the internal CRM integration
    actor_id = self.user_id or AUTOMATED_PIPELINE_USER_ID
    setup_service = InternalCRMSetupService(
        self.session, self.organisation_id, actor_id
    )
    integration = await setup_service.get_internal_integration()
    if not integration:
        return None

    # Deterministic external_id from email so re-runs are idempotent
    external_id = str(uuid5(NAMESPACE_URL, f"manual-contact:{email_normalized}"))

    now = datetime.now(UTC)
    contact = CRMContact(
        organisation_id=self.organisation_id,
        integration_id=integration.integration_id,
        external_id=external_id,
        email=email_normalized,
        full_name=name,
        job_title=job_title,
        phone=phone_number,
        synced_at=now,
        last_seen_at=now,
    )
    self.session.add(contact)
    await self.session.flush()
    return contact.contact_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/service/crm/test_stakeholder_contact_linking.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/service/crm/insights/stakeholders_service.py tests/service/crm/test_stakeholder_contact_linking.py
git commit -m "feat: add _ensure_contact_for_stakeholder to StakeholdersService"
```

---

### Task 3: Wire contact creation into `create_stakeholder()` + remove auto-reactivation

**Why:** Task 2 built the method; this task calls it from the user-facing flow. Every manually-added stakeholder now gets a linked `CRMContact` and `created_by_user_id`. Also removes the auto-reactivation behavior — when a REMOVED/REJECTED stakeholder is re-added, the API now returns the stakeholder's current state and ID (409 with data) so the frontend can let the user decide whether to re-approve via the approve endpoint, rather than silently reactivating.

**Files:**
- Modify: `src/service/crm/insights/stakeholders_service.py` — `create_stakeholder()` method

- [ ] **Step 1: Update `create_stakeholder()` to call `_ensure_contact_for_stakeholder()`**

In `create_stakeholder()`, after building `new_stakeholder` (around line 430) but before `self.session.add(new_stakeholder)`, insert:

```python
        # Link to CRMContact (find existing or create new under internal CRM)
        contact_id = await self._ensure_contact_for_stakeholder(
            email=email_normalized,
            name=request.name,
            job_title=request.job_title,
            phone_number=request.phone_number,
        )
        new_stakeholder.contact_id = contact_id
        new_stakeholder.created_by_user_id = self.user_id
```

Also update the reactivation path (around line 385-415). After the reactivation block and before `await self.session.commit()`, add:

```python
                # Link to CRMContact if not already linked
                if not existing_stakeholder.contact_id:
                    contact_id = await self._ensure_contact_for_stakeholder(
                        email=email_normalized,
                        name=request.name or existing_stakeholder.name,
                        job_title=request.job_title or existing_stakeholder.job_title,
                        phone_number=request.phone_number or existing_stakeholder.phone_number,
                    )
                    existing_stakeholder.contact_id = contact_id
```

Remove the comment `contact_id=None,  # Manual adds have no CRM contact` on the `new_stakeholder` creation and replace with `contact_id=contact_id,`.

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `uv run pytest tests/service/crm/ -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add src/service/crm/insights/stakeholders_service.py
git commit -m "feat: wire contact creation into create_stakeholder flow"
```

---

### Task 4: Add `_ensure_deal_participant()` for internal CRM deals

**Why:** The calendar and email matching pipelines rely on `CRMDealParticipant` records to build customer domain lists. For deals managed by the internal CRM (not HubSpot/Salesforce), creating a participant record ensures the matching pipelines can discover other meeting attendees from the same company. For external CRM deals, we skip this to avoid conflicting with the next CRM sync.

**Files:**
- Modify: `src/service/crm/insights/stakeholders_service.py`
- Test: `tests/service/crm/test_stakeholder_contact_linking.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/service/crm/test_stakeholder_contact_linking.py`:

```python
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
)


@pytest.mark.asyncio
async def test_ensure_deal_participant_creates_for_internal_deal() -> None:
    """Create CRMDealParticipant when deal belongs to internal CRM."""
    deal = SimpleNamespace(
        deal_id=DEAL_ID,
        integration_id=INTEGRATION_ID,
        organisation_id=ORG_ID,
    )
    internal_integration = SimpleNamespace(integration_id=INTEGRATION_ID)
    session = FakeSession(internal_integration=internal_integration)
    service = StakeholdersService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    contact_id = uuid4()
    created = await service._ensure_deal_participant(
        deal=deal,
        contact_id=contact_id,
        email="buyer@customer.com",
        name="Buyer Name",
    )

    assert created is True
    participants = [obj for obj in session.added if isinstance(obj, CRMDealParticipant)]
    assert len(participants) == 1
    assert participants[0].related_email == "buyer@customer.com"


@pytest.mark.asyncio
async def test_ensure_deal_participant_skips_external_deal() -> None:
    """Skip CRMDealParticipant when deal belongs to external CRM."""
    external_integration_id = uuid4()
    deal = SimpleNamespace(
        deal_id=DEAL_ID,
        integration_id=external_integration_id,
        organisation_id=ORG_ID,
    )
    internal_integration = SimpleNamespace(integration_id=INTEGRATION_ID)
    session = FakeSession(internal_integration=internal_integration)
    service = StakeholdersService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    created = await service._ensure_deal_participant(
        deal=deal,
        contact_id=uuid4(),
        email="buyer@customer.com",
        name="Buyer Name",
    )

    assert created is False
    participants = [obj for obj in session.added if isinstance(obj, CRMDealParticipant)]
    assert len(participants) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/service/crm/test_stakeholder_contact_linking.py::test_ensure_deal_participant_creates_for_internal_deal -v`
Expected: FAIL — no method `_ensure_deal_participant`

- [ ] **Step 3: Implement `_ensure_deal_participant()`**

Add these imports to `stakeholders_service.py`:

```python
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
```

Add the method:

```python
async def _ensure_deal_participant(
    self,
    deal: CRMDeal,
    contact_id: UUID,
    email: str,
    name: str | None,
) -> bool:
    """Create a CRMDealParticipant if the deal belongs to the internal CRM.

    Returns True if a participant was created, False if skipped.
    """
    actor_id = self.user_id or AUTOMATED_PIPELINE_USER_ID
    setup_service = InternalCRMSetupService(
        self.session, self.organisation_id, actor_id
    )
    integration = await setup_service.get_internal_integration()
    if not integration or deal.integration_id != integration.integration_id:
        return False

    # Check if participant already exists
    email_normalized = email.lower().strip()
    external_id = str(uuid5(NAMESPACE_URL, f"manual-contact:{email_normalized}"))
    stmt = select(CRMDealParticipant).where(
        col(CRMDealParticipant.deal_id) == deal.deal_id,
        col(CRMDealParticipant.related_email) == email_normalized,
        col(CRMDealParticipant.is_deleted).is_(False),
    )
    result = await self.session.execute(stmt)
    if result.scalars().first():
        return False

    now = datetime.now(UTC)
    participant_external_id = str(uuid5(
        NAMESPACE_URL,
        json.dumps({
            "deal_id": str(deal.deal_id),
            "participant_type": CRMParticipantType.CONTACT.value,
            "related_external_id": external_id,
            "relationship_role": "",
        }, sort_keys=True, separators=(",", ":")),
    ))

    participant = CRMDealParticipant(
        organisation_id=self.organisation_id,
        integration_id=integration.integration_id,
        external_id=participant_external_id,
        deal_id=deal.deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_external_id=external_id,
        related_local_id=contact_id,
        related_name=name,
        related_email=email_normalized,
        synced_at=now,
        source_modified_at=now,
        last_seen_at=now,
        data_payload={"source": "manual_stakeholder"},
    )
    self.session.add(participant)
    return True
```

- [ ] **Step 4: Wire into `create_stakeholder()` — after contact creation**

In `create_stakeholder()`, after the `_ensure_contact_for_stakeholder` call for new stakeholders, add:

```python
        # Create CRMDealParticipant if internal CRM deal
        if contact_id:
            deal = await self.session.get(CRMDeal, deal_id)
            if deal and deal.organisation_id == self.organisation_id:
                await self._ensure_deal_participant(
                    deal=deal,
                    contact_id=contact_id,
                    email=email_normalized,
                    name=request.name,
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/service/crm/test_stakeholder_contact_linking.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/service/crm/insights/stakeholders_service.py tests/service/crm/test_stakeholder_contact_linking.py
git commit -m "feat: create CRMDealParticipant for internal CRM deals on manual stakeholder add"
```

---

### Task 5: Soft-delete CRMDealParticipant on stakeholder removal

**Why:** If we create a `CRMDealParticipant` when a stakeholder is added (Task 4), we must clean it up when the stakeholder is removed. Otherwise, the removed person's domain would continue to feed into matching pipelines, potentially discovering unwanted stakeholders. Only internal CRM participants are touched — external CRM participants are never modified.

**Files:**
- Modify: `src/service/crm/insights/stakeholders_service.py` — `remove_stakeholder()` method
- Test: `tests/service/crm/test_stakeholder_contact_linking.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/service/crm/test_stakeholder_contact_linking.py`:

```python
@pytest.mark.asyncio
async def test_remove_stakeholder_soft_deletes_internal_participant() -> None:
    """Removing a stakeholder should soft-delete the matching internal CRM participant."""
    internal_integration = SimpleNamespace(integration_id=INTEGRATION_ID)
    participant = SimpleNamespace(
        participant_id=uuid4(),
        deal_id=DEAL_ID,
        integration_id=INTEGRATION_ID,
        related_email="buyer@customer.com",
        is_deleted=False,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )

    class RemovalFakeSession(FakeSession):
        async def execute(self, stmt):
            compiled = str(stmt).lower()
            if "knowledge_integrations" in compiled:
                return FakeScalarResult(internal_integration)
            if "crm_deal_participants" in compiled:
                return FakeScalarResult(participant)
            return await super().execute(stmt)

    session = RemovalFakeSession()
    service = StakeholdersService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    await service._soft_delete_internal_participant(DEAL_ID, "buyer@customer.com")

    assert participant.is_deleted is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/service/crm/test_stakeholder_contact_linking.py::test_remove_stakeholder_soft_deletes_internal_participant -v`
Expected: FAIL — no method `_soft_delete_internal_participant`

- [ ] **Step 3: Implement `_soft_delete_internal_participant()`**

Add to `StakeholdersService`:

```python
async def _soft_delete_internal_participant(
    self,
    deal_id: UUID,
    email: str,
) -> None:
    """Soft-delete the CRMDealParticipant for a removed stakeholder.

    Only deletes participants belonging to the internal CRM integration.
    External CRM participants are never touched.
    """
    actor_id = self.user_id or AUTOMATED_PIPELINE_USER_ID
    setup_service = InternalCRMSetupService(
        self.session, self.organisation_id, actor_id
    )
    integration = await setup_service.get_internal_integration()
    if not integration:
        return

    email_normalized = email.lower().strip()
    stmt = select(CRMDealParticipant).where(
        col(CRMDealParticipant.deal_id) == deal_id,
        col(CRMDealParticipant.integration_id) == integration.integration_id,
        col(CRMDealParticipant.related_email) == email_normalized,
        col(CRMDealParticipant.is_deleted).is_(False),
    )
    result = await self.session.execute(stmt)
    participant = result.scalars().first()
    if participant:
        participant.is_deleted = True
        now = datetime.now(UTC)
        participant.source_modified_at = now
        participant.synced_at = now
        participant.last_seen_at = now
```

- [ ] **Step 4: Wire into `remove_stakeholder()`**

In the `remove_stakeholder()` method, after `stakeholder.approval_status = ApprovalStatus.REMOVED` (line 312) and before the timeline event deletion block, add:

```python
        # Soft-delete matching internal CRM participant
        if stakeholder.email:
            await self._soft_delete_internal_participant(deal_id, stakeholder.email)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/service/crm/test_stakeholder_contact_linking.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/service/crm/insights/stakeholders_service.py tests/service/crm/test_stakeholder_contact_linking.py
git commit -m "feat: soft-delete internal CRM participant on stakeholder removal"
```

---

### Task 6: Fix `_collect_customer_domains()` in deal_meeting_matcher

**Why:** This is the root cause of the original bug. When a deal has meetings with 4 attendees but no new stakeholders are discovered, it's because `_collect_customer_domains()` only reads from `CRMDealParticipant`. If a stakeholder was added manually or the deal is on an external CRM (where we can't create participants), the domain is invisible. By also reading from approved `DealStakeholder` records, any stakeholder — regardless of source — contributes to meeting attendee discovery.

**Files:**
- Modify: `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py`
- Test: `tests/workers/deal_worker/services/test_customer_domain_collection.py`

- [ ] **Step 1: Write the failing test**

Create `tests/workers/deal_worker/services/test_customer_domain_collection.py`:

```python
"""Tests for customer domain collection from DealStakeholder records."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher import (
    DealMeetingMatcher,
)


ORG_ID = uuid4()


def _make_participant(email: str, ptype=CRMParticipantType.CONTACT):
    return SimpleNamespace(
        participant_type=ptype,
        related_email=email,
    )


def _make_deal(participants=None, primary_contact_email=None):
    return SimpleNamespace(
        deal_id=uuid4(),
        primary_contact_email=primary_contact_email,
        participants=participants or [],
    )


class FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


@pytest.mark.asyncio
async def test_collect_customer_domains_includes_stakeholder_emails() -> None:
    """Domains from approved DealStakeholder records should be included."""
    deal = _make_deal(
        participants=[_make_participant("crm@acme.com")],
    )

    # Session returns stakeholder emails when queried
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=FakeScalarResult(["manual@newco.com", "other@newco.com"])
    )

    matcher = DealMeetingMatcher(ORG_ID, session)
    domains = await matcher._collect_customer_domains(session, deal)

    assert "acme.com" in domains
    assert "newco.com" in domains


@pytest.mark.asyncio
async def test_collect_customer_domains_without_stakeholders() -> None:
    """Should still work with only CRM participants (no stakeholders)."""
    deal = _make_deal(
        participants=[_make_participant("contact@bigcorp.com")],
    )

    session = AsyncMock()
    session.execute = AsyncMock(return_value=FakeScalarResult([]))

    matcher = DealMeetingMatcher(ORG_ID, session)
    domains = await matcher._collect_customer_domains(session, deal)

    assert "bigcorp.com" in domains
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/workers/deal_worker/services/test_customer_domain_collection.py -v`
Expected: FAIL — `_collect_customer_domains()` doesn't accept `session` parameter

- [ ] **Step 3: Update `_collect_customer_domains()` to be async and include stakeholder domains**

In `deal_meeting_matcher.py`, change the method signature and add the stakeholder query:

```python
async def _collect_customer_domains(
    self, session: AsyncSession, deal: CRMDeal
) -> set[str]:
    """Collect customer domains from deal participants and approved stakeholders.

    Sources:
    1. CRMDealParticipant records of type CONTACT
    2. deal.primary_contact_email
    3. Approved DealStakeholder records (covers MANUAL, CALENDAR_DISCOVERY, etc.)
    """
    domains: set[str] = set()

    # From primary contact
    if deal.primary_contact_email:
        domain = self._extract_domain(deal.primary_contact_email)
        if domain:
            domains.add(domain)

    # From CONTACT participants
    for participant in self._get_participants(deal):
        if participant.participant_type != CRMParticipantType.CONTACT:
            continue
        if participant.related_email:
            domain = self._extract_domain(participant.related_email)
            if domain:
                domains.add(domain)

    # From approved DealStakeholder records
    from src.domain.models.organisation_tables.sync.crm.stakeholders import (
        ApprovalStatus,
        DealStakeholder,
    )
    stakeholder_stmt = select(col(DealStakeholder.email)).where(
        col(DealStakeholder.deal_id) == deal.deal_id,
        col(DealStakeholder.is_deleted).is_(False),
        col(DealStakeholder.approval_status) == ApprovalStatus.APPROVED,
    )
    stakeholder_result = await session.execute(stakeholder_stmt)
    for email in stakeholder_result.scalars().all():
        if email:
            domain = self._extract_domain(email)
            if domain:
                domains.add(domain)

    return domains
```

- [ ] **Step 4: Update all callers of `_collect_customer_domains()`**

Search for all calls to `_collect_customer_domains` in `deal_meeting_matcher.py` and update them to pass `session` and `await` the result. There should be one call in the main matching loop — likely in a method that already has access to `session`.

The call looks like:
```python
# Before:
customer_domains = self._collect_customer_domains(deal)

# After:
customer_domains = await self._collect_customer_domains(session, deal)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/workers/deal_worker/services/test_customer_domain_collection.py -v`
Expected: All PASS

Run: `uv run pytest tests/workers/deal_worker/ -v`
Expected: No regressions

- [ ] **Step 6: Commit**

```bash
git add src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py tests/workers/deal_worker/services/test_customer_domain_collection.py
git commit -m "fix: collect customer domains from DealStakeholder in meeting matcher"
```

---

### Task 7: Fix `_collect_company_domains()` in deal_email_matcher

**Why:** Same bug as Task 6, but in the email matching pipeline. The email matcher uses `_collect_company_domains()` to decide which email threads are relevant to a deal. Without stakeholder domains, emails from manually-added stakeholders' companies are never associated to the deal — even though those people are clearly involved.

**Files:**
- Modify: `src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py`
- Test: `tests/workers/deal_worker/services/test_customer_domain_collection.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/workers/deal_worker/services/test_customer_domain_collection.py`:

```python
from src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher import (
    DealEmailMatcher,
)


@pytest.mark.asyncio
async def test_email_matcher_collect_company_domains_includes_stakeholders() -> None:
    """Email matcher should also collect domains from approved stakeholders."""
    deal = _make_deal(
        participants=[_make_participant("crm@acme.com")],
    )

    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=FakeScalarResult(["manual@partner.com"])
    )

    matcher = DealEmailMatcher(ORG_ID, session)
    domains = await matcher._collect_company_domains(session, deal)

    assert "acme.com" in domains
    assert "partner.com" in domains
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/workers/deal_worker/services/test_customer_domain_collection.py::test_email_matcher_collect_company_domains_includes_stakeholders -v`
Expected: FAIL

- [ ] **Step 3: Update `_collect_company_domains()` in `deal_email_matcher.py`**

Apply the same pattern as Task 6: make the method async, add `session` parameter, add stakeholder query.

```python
async def _collect_company_domains(
    self, session: AsyncSession, deal: CRMDeal
) -> set[str]:
    """Collect customer domains from deal participants and approved stakeholders."""
    domains: set[str] = set()

    # From primary contact
    if deal.primary_contact_email:
        domain = self._extract_domain(deal.primary_contact_email)
        if domain:
            domains.add(domain)

    # From CONTACT participants
    participants = cast(list[CRMDealParticipant] | None, getattr(deal, "participants", None))
    if participants:
        for participant in participants:
            if participant.participant_type != CRMParticipantType.CONTACT:
                continue
            if participant.related_email:
                domain = self._extract_domain(participant.related_email)
                if domain:
                    domains.add(domain)

    # From approved DealStakeholder records
    from src.domain.models.organisation_tables.sync.crm.stakeholders import (
        ApprovalStatus,
        DealStakeholder,
    )
    stakeholder_stmt = select(col(DealStakeholder.email)).where(
        col(DealStakeholder.deal_id) == deal.deal_id,
        col(DealStakeholder.is_deleted).is_(False),
        col(DealStakeholder.approval_status) == ApprovalStatus.APPROVED,
    )
    stakeholder_result = await session.execute(stakeholder_stmt)
    for email in stakeholder_result.scalars().all():
        if email:
            domain = self._extract_domain(email)
            if domain:
                domains.add(domain)

    return domains
```

- [ ] **Step 4: Update all callers of `_collect_company_domains()`**

Search for calls in `deal_email_matcher.py`. There should be two:
1. In `_build_stakeholder_maps()` (around line 361): `info.company_domains = self._collect_company_domains(deal)` → `info.company_domains = await self._collect_company_domains(session, deal)`
2. In `match_for_deal()` (around line 919): same pattern

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/workers/deal_worker/services/test_customer_domain_collection.py -v`
Expected: All PASS

Run: `uv run pytest tests/workers/deal_worker/ -v`
Expected: No regressions

- [ ] **Step 6: Commit**

```bash
git add src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py tests/workers/deal_worker/services/test_customer_domain_collection.py
git commit -m "fix: collect company domains from DealStakeholder in email matcher"
```

---

### Task 8: Link contact_id in discovery paths

**Why:** When calendar or email matching discovers a new stakeholder from a meeting attendee, it currently creates the `DealStakeholder` with `contact_id=NULL` — even if a `CRMContact` with that email already exists. This is a missed opportunity: linking the contact gives the discovered stakeholder immediate access to name, company, and job title from the CRM. The AI agent tool has the same gap.

**Files:**
- Modify: `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py` — `_discover_stakeholders_from_event()`
- Modify: `src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py` — `_discover_stakeholders_from_email()`
- Modify: `src/agents/tools/crm/create_stakeholder_if_missing.py`

- [ ] **Step 1: Add contact_id lookup to calendar discovery**

In `deal_meeting_matcher.py`, method `_discover_stakeholders_from_event()`, after the `DealStakeholder(...)` creation (around line 385-397) and before `session.add(stakeholder)`, add:

```python
            # Link to existing CRMContact if one exists
            contact_stmt = select(col(CRMContact.contact_id)).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.email) == email,
            )
            contact_result = await session.execute(contact_stmt)
            contact_id = contact_result.scalar_one_or_none()
            if contact_id:
                stakeholder.contact_id = contact_id

            # Track automated source
            stakeholder.created_by_user_id = UUID("00000000-0000-0000-0000-000000000001")
```

Add the import at the top of `deal_meeting_matcher.py`:

```python
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
```

- [ ] **Step 2: Add contact_id lookup to email discovery**

In `deal_email_matcher.py`, method `_discover_stakeholders_from_email()`, after the `DealStakeholder(...)` creation (around line 262-274) and before `session.add(stakeholder)`, add:

```python
            # Link to existing CRMContact if one exists
            contact_stmt = select(col(CRMContact.contact_id)).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.email) == participant_email,
            )
            contact_result = await session.execute(contact_stmt)
            found_contact_id = contact_result.scalar_one_or_none()
            if found_contact_id:
                stakeholder.contact_id = found_contact_id

            # Track automated source
            stakeholder.created_by_user_id = UUID("00000000-0000-0000-0000-000000000001")
```

Add the import at the top of `deal_email_matcher.py`:

```python
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
```

- [ ] **Step 3: Add contact_id lookup to AI agent tool**

In `src/agents/tools/crm/create_stakeholder_if_missing.py`, inside the creation block (around line 109-138), after building `stakeholder_data` dict and before creating the model, add a lookup:

```python
            # Link to existing CRMContact if one exists
            contact_stmt = select(col(CRMContact.contact_id)).where(
                col(CRMContact.organisation_id) == organisation_id,
                col(CRMContact.email) == email,
            )
            contact_result = await session.execute(contact_stmt)
            found_contact_id = contact_result.scalar_one_or_none()
            stakeholder_data["contact_id"] = found_contact_id
```

Add the import:

```python
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
```

- [ ] **Step 4: Run all tests**

Run: `uv run pytest tests/workers/deal_worker/ tests/service/crm/ tests/agents/tools/crm/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py src/agents/tools/crm/create_stakeholder_if_missing.py
git commit -m "feat: link contact_id in calendar/email discovery and AI agent tool"
```

---

### Task 9: Fix dead log line in deal_participants_handler

**Why:** When a user adds or removes a participant (Contact/Team member) via the API, the handler logs "Triggering stakeholder sync" but never actually dispatches the Celery task. This means the stakeholder table doesn't update until the next Celery Beat cycle (up to 15 minutes). By wiring in the actual task dispatch, stakeholder sync happens immediately after participant changes.

**Files:**
- Modify: `src/api/routers/crm/deal_participants_handler.py:99-101`

- [ ] **Step 1: Add stakeholder sync trigger after participant addition**

In `deal_participants_handler.py`, the handler `add_deal_participants()` has a dead log line at line 99-101:

```python
        if contact_changed:
            logger.debug("Triggering stakeholder sync after participant addition")
```

Replace with actual task dispatch:

```python
        if contact_changed:
            try:
                from src.workers.deal_worker.client import send_deal_task_async
                from src.workers.deal_worker.services.deal_change_detector import (
                    StakeholderChangeType,
                )

                await send_deal_task_async(
                    "stakeholder.sync_deal_stakeholders",
                    kwargs={
                        "organisation_id": str(service.organisation_id),
                        "deal_id": str(deal_id),
                        "change_type": StakeholderChangeType.PARTICIPANT_ADDED.value,
                        "trigger_rematching": True,
                    },
                )
            except Exception:
                logger.warning(
                    "Failed to trigger stakeholder sync after participant addition",
                    exc_info=True,
                )
```

Do the same for the removal handler at line 154-155, replacing the dead log with:

```python
        if contact_changed:
            try:
                from src.workers.deal_worker.client import send_deal_task_async
                from src.workers.deal_worker.services.deal_change_detector import (
                    StakeholderChangeType,
                )

                await send_deal_task_async(
                    "stakeholder.sync_deal_stakeholders",
                    kwargs={
                        "organisation_id": str(service.organisation_id),
                        "deal_id": str(deal_id),
                        "change_type": StakeholderChangeType.PARTICIPANT_REMOVED.value,
                        "trigger_rematching": True,
                    },
                )
            except Exception:
                logger.warning(
                    "Failed to trigger stakeholder sync after participant removal",
                    exc_info=True,
                )
```

- [ ] **Step 2: Verify StakeholderChangeType has the needed values**

Read `src/workers/deal_worker/services/deal_change_detector.py` to confirm `PARTICIPANT_ADDED` and `PARTICIPANT_REMOVED` exist. If they don't, use an appropriate existing value like `MANUAL_REFRESH`.

- [ ] **Step 3: Commit**

```bash
git add src/api/routers/crm/deal_participants_handler.py
git commit -m "fix: trigger stakeholder sync when participants are added/removed via API"
```

---

### Task 10: Create backfill script for existing orphaned stakeholders

**Why:** All stakeholders created before this fix have `contact_id=NULL`. Without a backfill, existing deals won't benefit from the improved domain matching until someone manually re-adds those stakeholders. This one-time script creates `CRMContact` records for MANUAL stakeholders and links existing contacts to discovery-source stakeholders — bringing the entire database up to date.

**Files:**
- Create: `scripts/maintenance/backfill_stakeholder_contacts.py`

- [ ] **Step 1: Create the backfill script**

Create `scripts/maintenance/backfill_stakeholder_contacts.py`:

```python
#!/usr/bin/env python3
"""Backfill CRMContact records for existing manual stakeholders.

For MANUAL stakeholders with contact_id=NULL:
- If CRMContact exists by email: link contact_id
- If not: create CRMContact under internal CRM integration

For CALENDAR_DISCOVERY / EMAIL_DISCOVERY stakeholders with contact_id=NULL:
- If CRMContact exists by email: link contact_id
- If not: skip (discovery sources don't create contacts)

Usage:
    uv run python -m scripts.maintenance.backfill_stakeholder_contacts --all
    uv run python -m scripts.maintenance.backfill_stakeholder_contacts --org-id <UUID>
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from dotenv import load_dotenv
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DealStakeholder,
    StakeholderSource,
)
from src.infra.database.sessions import (
    acquire_master_session,
    acquire_org_async_session,
)
from src.infra.utils.resource_naming import get_schema_name
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

SYSTEM_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill CRMContact for orphaned stakeholders.")
    parser.add_argument("--org-id", type=UUID, help="Single org to process.")
    parser.add_argument("--all", action="store_true", help="Process all approved orgs.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without committing.")
    return parser.parse_args()


def load_org_ids(org_id: UUID | None, run_all: bool) -> list[UUID]:
    session_gen = acquire_master_session()
    session = next(session_gen)
    try:
        if run_all:
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
        elif org_id:
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.organisation_id) == org_id
            )
        else:
            return []
        result = session.execute(stmt)
        return [row[0] for row in result.all()]
    finally:
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()


async def backfill_org(org_id: UUID, dry_run: bool) -> dict[str, int]:
    stats = {"linked": 0, "created": 0, "skipped": 0, "errors": 0}

    async for session in acquire_org_async_session(org_id):
        # Get internal CRM integration
        setup = InternalCRMSetupService(session, org_id, SYSTEM_USER_ID)
        integration = await setup.get_internal_integration()

        # Find all stakeholders with no contact_id
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.organisation_id) == org_id,
            col(DealStakeholder.contact_id).is_(None),
            col(DealStakeholder.is_deleted).is_(False),
        )
        result = await session.execute(stmt)
        orphans = list(result.scalars().all())

        if not orphans:
            print(f"  No orphaned stakeholders found")
            return stats

        print(f"  Found {len(orphans)} orphaned stakeholder(s)")

        for stakeholder in orphans:
            if not stakeholder.email:
                stats["skipped"] += 1
                continue

            email = stakeholder.email.lower().strip()

            # Look up existing contact by email
            contact_stmt = select(CRMContact).where(
                col(CRMContact.organisation_id) == org_id,
                col(CRMContact.email) == email,
            )
            contact_result = await session.execute(contact_stmt)
            contact = contact_result.scalars().first()

            if contact:
                stakeholder.contact_id = contact.contact_id
                stats["linked"] += 1
                if not dry_run:
                    print(f"    Linked: {email} -> contact {contact.contact_id}")
            elif stakeholder.source == StakeholderSource.MANUAL:
                if not integration:
                    print(f"    Skip (no internal CRM): {email}")
                    stats["skipped"] += 1
                    continue

                external_id = str(uuid5(NAMESPACE_URL, f"manual-contact:{email}"))
                now = datetime.now(UTC)
                new_contact = CRMContact(
                    organisation_id=org_id,
                    integration_id=integration.integration_id,
                    external_id=external_id,
                    email=email,
                    full_name=stakeholder.name,
                    job_title=stakeholder.job_title,
                    phone=stakeholder.phone_number,
                    synced_at=now,
                    last_seen_at=now,
                )
                session.add(new_contact)
                await session.flush()
                stakeholder.contact_id = new_contact.contact_id
                stats["created"] += 1
                if not dry_run:
                    print(f"    Created: {email} -> contact {new_contact.contact_id}")
            else:
                # Discovery/AI source — don't create, just skip
                stats["skipped"] += 1

        if not dry_run:
            await session.commit()
            print(f"  Committed changes")
        else:
            print(f"  DRY RUN — no changes committed")

    return stats


async def main_async(org_ids: list[UUID], dry_run: bool) -> None:
    totals = {"linked": 0, "created": 0, "skipped": 0, "errors": 0}

    for org_id in org_ids:
        print(f"\n[ORG] {org_id}")
        try:
            stats = await backfill_org(org_id, dry_run)
            for k in totals:
                totals[k] += stats[k]
        except Exception as exc:
            totals["errors"] += 1
            print(f"  ERROR: {exc}")

    print(
        f"\n[SUMMARY] linked={totals['linked']} created={totals['created']} "
        f"skipped={totals['skipped']} errors={totals['errors']}"
    )


def main() -> None:
    load_dotenv()
    args = parse_args()

    if not args.all and not args.org_id:
        print("Specify --all or --org-id <UUID>")
        return

    org_ids = load_org_ids(args.org_id, args.all)
    if not org_ids:
        print("No organisations found.")
        return

    print(f"Processing {len(org_ids)} organisation(s){'  [DRY RUN]' if args.dry_run else ''}")
    asyncio.run(main_async(org_ids, args.dry_run))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add scripts/maintenance/backfill_stakeholder_contacts.py
git commit -m "feat: add backfill script for orphaned stakeholder contacts"
```

---

### Task 11: Final verification

**Why:** All individual tasks have their own tests, but this step ensures everything works together — no import conflicts, no type errors across modules, and no lint violations. This is the gate before creating a PR.

**Files:** All modified files

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest tests/service/crm/ tests/workers/deal_worker/ tests/agents/tools/crm/ -v
```

Expected: All PASS

- [ ] **Step 2: Run linter**

```bash
uv run ruff check src/service/crm/insights/stakeholders_service.py src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py src/api/routers/crm/deal_participants_handler.py src/agents/tools/crm/create_stakeholder_if_missing.py scripts/maintenance/backfill_stakeholder_contacts.py
```

Fix any issues found.

- [ ] **Step 3: Run type checker**

```bash
uv run mypy src/service/crm/insights/stakeholders_service.py src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py
```

Fix any issues found.

- [ ] **Step 4: Commit any fixes from linting/type checking**

```bash
git add -u
git commit -m "fix: lint and type check fixes for stakeholder-contact linking"
```
