# CRM Stakeholder Modularity Followup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the stakeholder modularity cleanup by making stakeholder writes reuse canonical contact services and making agent tools reuse stakeholder services.

**Architecture:** `CRMDealParticipant` remains the canonical stakeholder storage row. `StakeholderWriteService` should own only stakeholder semantics over a participant; durable contact profile creation and updates should flow through `ContactReadService` and `ContactWriteService`. Agent tools should call `StakeholderReadService` and `StakeholderWriteService` instead of duplicating participant SQL.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async sessions, PostgreSQL tenant schemas, pytest, mypy, ruff.

---

## Current State

The participant/stakeholder split is materially better than the previous shape:

- `src/service/crm/participants/` now exists.
- `src/service/crm/internal/deal_participant_service.py` is a compatibility facade over `ParticipantWriteService`.
- `src/service/crm/insights/stakeholders_service.py` is a compatibility facade over `StakeholderReadService` and `StakeholderWriteService`.
- The old `deal_stakeholders` table has not been reintroduced.
- Focused participant/stakeholder tests pass.

The remaining modularity issues are narrower:

- `StakeholderWriteService` still creates and mutates `CRMContact` directly.
- Some stakeholder agent tools still query `CRMDealParticipant` directly.
- `StakeholderWriteService` is large because it mixes contact materialization, participant mutation, timeline creation, approval state, and bulk-add orchestration.

## Production Boundary Decision

Keep these responsibilities separate:

```text
ContactReadService
  read durable person profiles
  find existing contact by email

ContactWriteService
  create/update durable CRMContact rows
  own contact normalization, duplicate checks, integration ownership, future contact mutation events

ParticipantWriteService
  create/update/remove raw CRMDealParticipant roster rows
  own generic participant identity rules

StakeholderReadService
  expose stakeholder-shaped reads from contact participants

StakeholderWriteService
  coordinate stakeholder semantic writes
  delegate contact creation/profile update to contact services
  delegate raw participant creation/removal where possible
```

Do not add a new table. Do not move workflow dispatcher logic into this cleanup.

Keep the service contracts behavior-preserving for existing callers:

- Stakeholder list agent reads must preserve their current limit clamp and stable ordering.
- Stakeholder detail agent reads must fail closed when no deal scope is available, unless a
  deliberately named org-scoped read service method is added with tests.
- Contact upserts must merge nested `properties` instead of replacing existing contact metadata.

## Why This Is Needed

A stakeholder-created contact, API-created contact, agent-created contact, workflow-created contact, and WhatsApp-created contact are all the same CRM contact. If stakeholder code constructs `CRMContact` directly, the next contact rule change has to be patched in multiple places.

This matters for:

- duplicate prevention
- contact normalization
- future contact mutation events
- workflow-triggered contact writes
- agent tool consistency
- auditability when a meeting or workflow updates CRM contact data

## Non-Negotiable Rules

- `StakeholderWriteService` must not instantiate `CRMContact(...)`.
- `StakeholderWriteService` must not perform contact duplicate checks manually if the contact services expose the needed contract.
- `StakeholderWriteService` must not commit. Routers/tools/orchestrators own commit/rollback.
- Agent tools should not duplicate stakeholder visibility SQL unless a tool has a documented performance exception.
- Public stakeholder APIs continue to expose `participant_id`, not `stakeholder_id`.
- Job/research compatibility may continue to pass `stakeholder_id=<participant_id>` until worker schemas are renamed separately.

---

### Task 1: Add Contact Upsert Contract For Stakeholder Callers

**Files:**
- Modify: `src/service/crm/contacts/contracts.py`
- Modify: `src/service/crm/contacts/write_service.py`
- Test: `tests/service/crm/test_contact_write_service.py`

- [x] **Step 1: Write failing tests for contact find-or-create by email**

Add tests that prove stakeholder-like callers can reuse an existing contact and create a missing one without direct `CRMContact(...)` construction outside `ContactWriteService`.

```python
async def test_find_or_create_by_email_reuses_existing_contact(fake_session) -> None:
    service = ContactWriteService(fake_session, ORG_ID)
    existing = CRMContact(
        contact_id=CONTACT_ID,
        organisation_id=ORG_ID,
        integration_id=INTEGRATION_ID,
        email="buyer@example.com",
        full_name="Old Name",
        is_deleted=False,
    )
    fake_session.add_result(existing)

    result = await service.find_or_create_contact_by_email(
        email=" Buyer@Example.com ",
        name="New Name",
        context=ContactWriteContext(source="stakeholder"),
        profile_updates={"job_title": "CFO"},
    )

    assert result.contact.contact_id == CONTACT_ID
    assert result.created is False
    assert result.matched_by == "email"
    assert result.contact.full_name == "New Name"
    assert result.contact.job_title == "CFO"
```

```python
async def test_find_or_create_by_email_creates_missing_contact(fake_session) -> None:
    service = ContactWriteService(fake_session, ORG_ID)
    fake_session.add_result(None)
    fake_session.add_internal_integration(INTEGRATION_ID)

    result = await service.find_or_create_contact_by_email(
        email="buyer@example.com",
        name="Buyer Name",
        context=ContactWriteContext(source="stakeholder"),
        profile_updates={"phone": "+15555550100"},
    )

    assert result.created is True
    assert result.matched_by == "created"
    assert result.contact.email == "buyer@example.com"
    assert result.contact.full_name == "Buyer Name"
```

- [x] **Step 2: Run the focused failing tests**

Run:

```bash
uv run pytest tests/service/crm/test_contact_write_service.py -q
```

Expected before implementation: the new method is missing.

- [x] **Step 3: Add the contact write method**

Add this method to `ContactWriteService`:

```python
async def find_or_create_contact_by_email(
    self,
    *,
    email: str,
    name: str | None,
    context: ContactWriteContext | None = None,
    profile_updates: dict[str, Any] | None = None,
) -> ContactWriteResult:
    """Find by normalized email or create one internal CRM contact.

    Stakeholder, agent, workflow, and form callers use this path so contact
    normalization and future contact mutation events stay in one service.
    """
    normalized_email = email.strip().lower()
    stmt = select(CRMContact).where(
        col(CRMContact.organisation_id) == self.organisation_id,
        col(CRMContact.is_deleted).is_(False),
        func.lower(col(CRMContact.email)) == normalized_email,
    )
    result = await self.org_session.execute(stmt)
    existing = result.scalars().first()
    updates = dict(profile_updates or {})

    if existing:
        if name:
            existing.full_name = name.strip()
            first_name, last_name = self._split_name(name)
            existing.first_name = first_name
            existing.last_name = last_name
        properties = updates.pop("properties", None)
        if isinstance(properties, dict):
            existing.properties = {**(existing.properties or {}), **properties}
        for field, value in updates.items():
            if hasattr(existing, field):
                setattr(existing, field, value)
        self.org_session.add(existing)
        await self.org_session.flush()
        await self.org_session.refresh(existing)
        return ContactWriteResult(contact=existing, created=False, matched_by="email")

    return await self.create_contact(
        normalized_email,
        name or normalized_email,
        context=context,
        **updates,
    )
```

- [x] **Step 4: Run contact write tests**

Run:

```bash
uv run pytest tests/service/crm/test_contact_write_service.py -q
```

Expected: contact write tests pass.

---

### Task 2: Make Stakeholder Writes Delegate Contact Creation And Profile Updates

**Files:**
- Modify: `src/service/crm/participants/stakeholder_write_service.py`
- Test: `tests/service/crm/insights/test_stakeholders_service_participant_writes.py`
- Test: `tests/service/crm/test_stakeholder_contact_linking.py`

- [x] **Step 1: Add boundary tests that reject direct contact construction**

Add a test to `tests/service/crm/test_participant_stakeholder_service_boundaries.py`:

```python
def test_stakeholder_write_service_does_not_construct_contacts_directly() -> None:
    source = Path("src/service/crm/participants/stakeholder_write_service.py").read_text()
    assert "CRMContact(" not in source
    assert "ContactWriteService(" in source
```

- [x] **Step 2: Run the boundary test**

Run:

```bash
uv run pytest tests/service/crm/test_participant_stakeholder_service_boundaries.py -q
```

Expected before implementation: the new boundary test fails because `CRMContact(` still exists.

- [x] **Step 3: Replace `_ensure_contact_for_stakeholder` direct writes**

Change `_ensure_contact_for_stakeholder` so it delegates to `ContactWriteService.find_or_create_contact_by_email`.

```python
from src.service.crm.contacts.contracts import ContactWriteContext
from src.service.crm.contacts.write_service import ContactWriteService
```

```python
contact_writer = ContactWriteService(self.session, self.organisation_id)
result = await contact_writer.find_or_create_contact_by_email(
    email=email_normalized,
    name=name,
    context=ContactWriteContext(
        source="stakeholder",
        actor_user_id=self.user_id,
        source_label="Stakeholder",
        created_via="stakeholder_write_service",
    ),
    profile_updates={
        key: value
        for key, value in {
            "job_title": job_title,
            "phone": phone_number,
            "properties": {
                profile_key: profile_value
                for profile_key, profile_value in {
                    "department": department,
                    "external_links": external_links,
                }.items()
                if profile_value
            },
        }.items()
        if value
    },
)
return result.contact.contact_id
```

Keep the existing internal-domain and personal-email skip behavior before this delegation.

- [x] **Step 4: Keep participant snapshot updates local**

`StakeholderWriteService` may still copy contact fields into `CRMDealParticipant` because those are deal-local snapshots. Do not move these fields into `ContactWriteService`:

```text
related_name
related_email
job_title
department
phone_number
external_links
stance
decision_power
motivation
pain_points
keywords
temperature
approval_status
```

- [x] **Step 5: Run focused stakeholder write tests**

Run:

```bash
uv run pytest tests/service/crm/test_participant_stakeholder_service_boundaries.py tests/service/crm/insights/test_stakeholders_service_participant_writes.py tests/service/crm/test_stakeholder_contact_linking.py -q
```

Expected: tests pass, and `StakeholderWriteService` no longer constructs `CRMContact` directly.

---

### Task 3: Move Agent Stakeholder Reads Onto Stakeholder Services

**Files:**
- Modify: `src/agents/tools/crm/list_stakeholders.py`
- Modify: `src/agents/tools/crm/get_stakeholder_details.py`
- Modify: `src/agents/tools/crm/enrich_stakeholder.py`
- Test: `tests/service/agents/test_stakeholder_tools.py`

- [x] **Step 1: Add a tool boundary test**

Add a boundary test that keeps new stakeholder tools away from raw participant SQL:

```python
def test_stakeholder_tools_use_services_for_reads() -> None:
    for path in [
        Path("src/agents/tools/crm/list_stakeholders.py"),
        Path("src/agents/tools/crm/get_stakeholder_details.py"),
    ]:
        source = path.read_text()
        assert "select(CRMDealParticipant)" not in source
        assert "StakeholderReadService(" in source
```

- [x] **Step 2: Run the failing tool boundary test**

Run:

```bash
uv run pytest tests/service/agents/test_stakeholder_tools.py -q
```

Expected before implementation: the new boundary test fails for tools that still query `CRMDealParticipant` directly.

- [x] **Step 3: Replace list/detail raw SQL with `StakeholderReadService`**

Use the org session already acquired by each tool:

```python
service = StakeholderReadService(session, organisation_id, user_id)
result = await service.list_stakeholders_by_deal(
    actual_deal_id,
    include_pending=True,
    include_rejected=False,
    limit=effective_limit,
)
```

For detail:

```python
if not context.deal_id:
    logger.warning("get_stakeholder_details: no deal_id in context")
    return None

service = StakeholderReadService(session, organisation_id, user_id)
detail = await service.get_stakeholder_detail(context.deal_id, parsed_stakeholder_id)
```

Keep the tool input field name `stakeholder_id` only where the public tool contract already uses that language. The value remains `participant_id`.

- [x] **Step 4: Leave `enrich_stakeholder.py` for a separate write cleanup if needed**

`enrich_stakeholder.py` performs enrichment writes directly on `CRMDealParticipant`. If it cannot be moved safely in the same slice, add a boundary comment at the top of the write block:

```python
# This tool still writes enrichment fields directly to CRMDealParticipant.
# Move it to a stakeholder enrichment service before adding workflow-triggered enrichment.
```

Do not refactor enrichment and list/detail reads in the same risky edit unless the tests already isolate both paths.

- [x] **Step 5: Run focused agent tool tests**

Run:

```bash
uv run pytest tests/service/agents/test_stakeholder_tools.py tests/agents/tools/crm/test_upsert_stakeholder.py tests/agents/tools/crm/test_create_stakeholder_if_missing.py -q
```

Expected: stakeholder tools still return participant IDs and no `stakeholder_id` attribute regression returns.

---

### Task 4: Final Focused Verification

**Files:**
- No new production files.

- [x] **Step 1: Run participant/stakeholder service verification**

Run:

```bash
uv run pytest tests/service/crm/test_participant_stakeholder_service_boundaries.py tests/service/crm/insights/test_stakeholders_service_participant_reads.py tests/service/crm/insights/test_stakeholders_service_participant_writes.py tests/service/crm/test_deal_participant_service_internal_ids.py -q
```

Expected: all selected tests pass.

- [x] **Step 2: Run contact write verification**

Run:

```bash
uv run pytest tests/service/crm/test_contact_write_service.py tests/service/crm/test_contact_deal_link_service.py -q
```

Expected: contact write and contact-to-deal link tests pass.

- [x] **Step 3: Run focused lint/type checks only for touched paths**

Run:

```bash
uv run ruff check src/service/crm/contacts src/service/crm/participants src/agents/tools/crm/list_stakeholders.py src/agents/tools/crm/get_stakeholder_details.py tests/service/crm tests/service/agents/test_stakeholder_tools.py
```

Run:

```bash
uv run mypy --no-incremental src/service/crm/contacts src/service/crm/participants src/agents/tools/crm/list_stakeholders.py src/agents/tools/crm/get_stakeholder_details.py
```

Expected: no new lint/type failures in the touched paths.

## Success Criteria

- `StakeholderWriteService` no longer constructs `CRMContact` directly.
- Durable contact changes flow through `ContactWriteService`.
- Stakeholder list/detail agent tools use `StakeholderReadService`.
- Old stakeholder compatibility facades stay intact.
- No database tables or migrations are added for this cleanup.
- Public API/tool responses continue to expose `participant_id`.

## Known Deferred Cleanup

- Rename worker/job parameters from `stakeholder_id` to `participant_id` in a separate compatibility-safe worker contract change.
- Extract timeline event creation from `StakeholderWriteService` only after contact delegation is stable.
- Move stakeholder enrichment writes into a dedicated enrichment service before workflow-triggered enrichment is added.
