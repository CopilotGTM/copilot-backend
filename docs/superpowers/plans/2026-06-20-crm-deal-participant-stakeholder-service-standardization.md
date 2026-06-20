# CRM Deal Participant And Stakeholder Service Standardization Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create one canonical participant-backed deal-people module while preserving stakeholder as the product/API language.

**Architecture:** `CRMDealParticipant` is the storage source of truth. Deal participants are the generic deal roster rows; stakeholders are contact participants with stakeholder semantics such as approval status, stance, decision power, research, timeline events, and visible jobs. The first implementation is additive: create `src/service/crm/participants/`, route new code through it, and keep the old `DealParticipantService` and `StakeholdersService` paths as temporary compatibility facades until imports are migrated.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async sessions, PostgreSQL tenant schemas, Celery deal-worker, pytest, mypy, ruff.

---

## Starting Point

Current facts from this checkout:

- The legacy `deal_stakeholders` table has already been retired.
- `src/domain/models/organisation_tables/sync/crm/deals.py` defines `CRMDealParticipant`.
- `src/domain/models/organisation_tables/sync/crm/stakeholders.py` now contains stakeholder enums only.
- `src/service/crm/internal/deal_participant_service.py` owns raw participant writes:
  - add contacts/companies/users to an internal CRM deal
  - remove a participant
  - update participant role
  - update deal company snapshot when company participants change
- `src/service/crm/insights/stakeholders_service.py` owns stakeholder semantics on top of `CRMDealParticipant`:
  - list/detail stakeholder views
  - create manual stakeholder
  - add contacts as stakeholders
  - approve/reject/remove stakeholder
  - update stakeholder profile/behavior fields
  - trigger matching/sync helpers
- `src/service/crm/insights/participant_stakeholder_adapter.py` converts `CRMDealParticipant` plus optional `CRMContact` into stakeholder DTO dictionaries.
- `src/api/routers/crm/deal_participants_handler.py` calls `DealParticipantService`.
- `src/api/routers/crm/stakeholders_handler.py` calls `StakeholdersService`.
- Agents and workers still expose stakeholder language, but the ID they pass is `CRMDealParticipant.participant_id`.
- The account and contact service-standardization plans already established the vertical module pattern:
  - `src/service/crm/accounts/`
  - `src/service/crm/contacts/`

## Current Problem

The storage model is now unified, but the service model is still split by old history.

Current production risks:

- Two services can create or mutate the same `crm_deal_participants` rows.
- `DealParticipantService` treats participants as generic roster rows, while `StakeholdersService` treats contact participants as stakeholder state.
- Some transaction boundaries are still service-owned, especially inside `StakeholdersService`.
- Job dispatch is partly in routers and partly in stakeholder service helpers.
- New agent/workflow code has to choose between a raw participant service and a stakeholder service without a clear boundary.
- The old folder names imply stakeholder is a separate storage area under `insights`, which is no longer true.
- Stale `stakeholder_id` wording still exists in job/research code as a compatibility name for participant IDs.

## Core Decision

Use one vertical deal-people module:

```text
src/service/crm/participants/
  __init__.py
  contracts.py
  participant_read_service.py
  participant_write_service.py
  stakeholder_read_service.py
  stakeholder_write_service.py
  stakeholder_adapter.py
```

Keep product/API language as stakeholder:

```text
GET    /users/crm/deals/{deal_id}/stakeholders
POST   /users/crm/deals/{deal_id}/stakeholders
PATCH  /users/crm/deals/{deal_id}/stakeholders/{participant_id}
```

Keep storage and internal identity as participant:

```text
table: crm_deal_participants
primary key: participant_id
public response field: participant_id
legacy response field: no stakeholder_id
```

Do not create another `deal_stakeholders` table and do not add a shadow stakeholder table.

## Terminology Contract

| Term | Meaning | Storage |
| --- | --- | --- |
| Participant | Any entity attached to a deal: contact, company, user, ticket, line item, custom | `crm_deal_participants` |
| Contact participant | A participant where `participant_type = contact` | `crm_deal_participants` |
| Stakeholder | Product view of a contact participant that can be approved, rejected, researched, scored, analyzed, or shown in buying-committee UX | `crm_deal_participants` plus optional `crm_contacts` |
| Contact | Person profile record independent of one deal | `crm_contacts` |
| Account/company | Company profile record independent of one deal | `crm_companies` |

So the rule is:

```text
Every stakeholder is a contact participant.
Not every participant is a stakeholder.
```

Examples:

```text
Company linked to deal          -> participant, not stakeholder
Internal owner/user on deal     -> participant, not stakeholder
External buyer contact on deal  -> participant and stakeholder
```

## Ownership Rules

### `CRMDealParticipant` Owns Deal-Local State

These fields are deal-specific and should stay on the participant row:

```text
deal_id
participant_type
relationship_role
is_primary_contact
related_local_id
related_name
related_email
participant_source
approval_status
stance
decision_power
motivation
pain_points
keywords
last_contact_at
temperature
stakeholder_research fallback
last_researched_at fallback
is_deleted
```

### `CRMContact` Owns Durable Person Profile

These fields should be written to contact first when a contact exists, then snapshotted onto participant for list speed:

```text
email
full_name
job_title
phone
mobile_phone
department in properties
external_links in properties
contact_research
last_researched_at
```

### `CRMDeal` Owns Primary Deal Snapshot

Participant writes may update deal-level snapshots only when the business rule is clear:

```text
company_id/company_name/company_domain when company participant changes
primary_contact_id/primary_contact_name/primary_contact_email when primary contact participant changes
```

Do not let generic stakeholder updates silently change unrelated deal fields.

## Future Folder Pattern

After this plan, the CRM service layout should become:

```text
src/service/crm/
  accounts/
    contracts.py
    read_service.py
    write_service.py
  contacts/
    contracts.py
    read_service.py
    write_service.py
    deal_link_service.py
  participants/
    contracts.py
    participant_read_service.py
    participant_write_service.py
    stakeholder_read_service.py
    stakeholder_write_service.py
    stakeholder_adapter.py
  access/
    accounts_access_service.py       # temporary facade
    contacts_access_service.py       # temporary facade
  internal/
    deal_participant_service.py      # temporary facade after migration
    deal_write_service.py
  insights/
    stakeholders_service.py          # temporary facade after migration
    participant_stakeholder_adapter.py # temporary re-export after migration
```

Do not migrate deals into `src/service/crm/deals/` in this plan.

## Target Service Boundaries

### Participant Read Service

`ParticipantReadService` should own raw roster reads:

```text
list deal participants
get participant by id
check whether deal has active participants
```

It should not return stakeholder DTOs.

### Participant Write Service

`ParticipantWriteService` should own raw participant membership writes:

```text
add_participants()
remove_participant()
update_participant_role()
```

It may create or restore `CRMDealParticipant` rows.

It must not:

```text
create CRMContact
dispatch stakeholder research
create visible DealJobs
emit stakeholder timeline events except low-level participant audit later
```

### Stakeholder Read Service

`StakeholderReadService` should own stakeholder product reads:

```text
list_stakeholders_by_deal()
get_stakeholder()
get_stakeholder_detail()
list_available_contacts()
```

It reads from `CRMDealParticipant`, joins/resolves `CRMContact`, and returns stakeholder DTOs.

### Stakeholder Write Service

`StakeholderWriteService` should own stakeholder semantic writes:

```text
create_stakeholder()
add_contacts_as_stakeholders()
update_stakeholder()
approve_stakeholder()
reject_stakeholder()
remove_stakeholder()
```

It may:

```text
create/find CRMContact for manual stakeholder
create/update CRMDealParticipant contact rows
write stakeholder timeline events
write contact profile fields and participant behavior fields
```

It must not:

```text
own visible job creation policy
own Celery dispatch policy
commit transactions after this migration
```

Visible jobs and worker dispatch stay at orchestration boundaries for now:

```text
API router
agent tool
future workflow dispatcher
```

## Temporary Compatibility Surfaces

Keep these files initially:

```text
src/service/crm/internal/deal_participant_service.py
src/service/crm/insights/stakeholders_service.py
src/service/crm/insights/participant_stakeholder_adapter.py
```

After the new module exists:

```text
DealParticipantService -> delegates to ParticipantWriteService
StakeholdersService    -> delegates to StakeholderReadService + StakeholderWriteService
participant_stakeholder_adapter.py -> re-exports from participants/stakeholder_adapter.py
```

Delete or move them only after:

```bash
rg -n "DealParticipantService|StakeholdersService|participant_stakeholder_adapter" src tests
```

proves there are no production imports that still need the compatibility path.

## Non-Negotiable Rules

- Do not recreate `deal_stakeholders`.
- Do not add a new stakeholder table.
- Do not expose `stakeholder_id` in new API/tool payloads.
- Keep `participant_id` as the canonical ID for stakeholder routes.
- Keep stakeholder as product/API language.
- Keep provider sync participant writers out of scope in the first slice.
- Every query and write must filter or set `organisation_id`.
- Internal participant writes must reject external CRM deals/contacts/companies as read-only.
- Contact participant identity should dedupe by contact ID when available, and by normalized email for contact rows.
- Contact participants should not duplicate by role; the same contact can be primary and decision-maker through flags/fields, not duplicate rows.
- Company/user participants may keep role-specific uniqueness if the existing behavior requires it.
- Services should not commit after the migration; routers/tools/workflows own commit/rollback.
- Job creation and Celery dispatch should not be hidden inside low-level participant writes.
- Stakeholder sync/research job parameters may still call the field `stakeholder_id` in old job tables, but the value must be `CRMDealParticipant.participant_id`.

## Loopholes To Close

### Loophole 1: Two Write Paths Create The Same Participant

Risk:

```text
POST /participants adds a contact participant.
POST /stakeholders adds the same email as a stakeholder.
```

Required behavior:

```text
same deal + same contact_id/email -> one active contact participant
stakeholder create restores or updates the existing participant instead of inserting another row
```

### Loophole 2: Role-Based Duplicates For Contacts

Risk:

```text
same contact added as decision_maker and primary_contact creates two rows
```

Required behavior:

```text
one contact participant row
is_primary_contact = true when primary role is present
relationship_role can preserve the most useful business role
```

### Loophole 3: Stakeholder Profile Writes Drift From Contact Profile

Risk:

```text
stakeholder update changes name/phone/title only on participant snapshot
contact detail later shows stale values
```

Required behavior:

```text
contact owns durable profile fields
participant keeps snapshots for deal-list reads
```

### Loophole 4: Low-Level Participant Writes Trigger Too Much Work

Risk:

```text
internal service add_participants() starts rematch, sync, visible jobs, and research
```

Required behavior:

```text
service returns a typed result with contact_participant_changed
router/tool/workflow decides which jobs to enqueue after commit
```

### Loophole 5: Existing Worker Naming Confuses Identity

Risk:

```text
worker arg stakeholder_id is assumed to be a legacy table ID
```

Required behavior:

```text
document and test that stakeholder_id job fields contain participant_id
new code should prefer variable name participant_id where possible
```

### Loophole 6: Transaction Ownership Is Mixed

Risk:

```text
service commits, router creates jobs, then a later failure cannot roll back consistently
```

Required behavior:

```text
write services flush/refresh only
router/tool commits
post-commit jobs dispatch only after the row is durable
```

### Loophole 7: Provider Sync Is Accidentally Changed

Risk:

```text
service cleanup changes HubSpot/Salesforce participant ingestion
```

Required behavior:

```text
first slice does not change src/service/integrations/crm/records/*/deals/participant_writer.py
provider writers can be reviewed later after the internal write boundary is stable
```

## Flow Examples

### Flow A: Add Raw Contact Participant From Contact App

```text
API request
  -> ParticipantWriteService.add_participants()
  -> validate internal deal
  -> validate internal contact
  -> find existing contact participant by contact_id/email
  -> create or restore CRMDealParticipant
  -> return ParticipantWriteResult(contact_participant_changed=true)
router commits
router dispatches stakeholder sync/rematch if contact_participant_changed
frontend refreshes deal participants/stakeholders
```

### Flow B: Manually Add Stakeholder

```text
API request
  -> StakeholderWriteService.create_stakeholder()
  -> reject internal team email
  -> find or create CRMContact when allowed
  -> find existing contact participant by contact_id/email
  -> create/restore/update CRMDealParticipant with stakeholder fields
  -> add stakeholder timeline event
  -> return StakeholderWriteResult
router commits
router creates visible processing jobs
router dispatches stakeholder research for participant_id
frontend refreshes stakeholders/timeline/jobs
```

### Flow C: Add Existing Contacts As Stakeholders

```text
API request with contact_ids
  -> StakeholderWriteService.add_contacts_as_stakeholders()
  -> load contacts in org
  -> skip missing contacts
  -> skip contacts without email
  -> skip existing active contact participants by contact_id/email
  -> create participant rows with source=CRM and approval_status=APPROVED
  -> add timeline events
  -> return added/skipped
router commits
router creates processing jobs and research for each added participant_id
```

### Flow D: Stakeholder Update

```text
API request PATCH /stakeholders/{participant_id}
  -> StakeholderWriteService.update_stakeholder()
  -> load participant by organisation_id + deal_id + participant_id + type=contact
  -> load linked CRMContact when available
  -> write profile fields to CRMContact
  -> snapshot profile fields to CRMDealParticipant
  -> write behavior fields to CRMDealParticipant
  -> return stakeholder DTO
router commits
```

### Flow E: Remove Participant Versus Remove Stakeholder

```text
DELETE /participants/{participant_id}
  -> raw participant membership removal
  -> is_deleted=true
  -> for contact participant, returns contact_participant_changed=true

DELETE /stakeholders/{participant_id}
  -> stakeholder semantic removal
  -> approval_status=REMOVED
  -> is_deleted=true
  -> timeline event STAKEHOLDER_REMOVED
```

Both touch the same row, but the product meaning differs.

## Task 0: Add Participant Module And Boundary Tests

**Files:**

- Create: `src/service/crm/participants/__init__.py`
- Create: `src/service/crm/participants/contracts.py`
- Create: `tests/service/crm/test_participant_stakeholder_service_boundaries.py`

- [ ] **Step 1: Add boundary tests**

Create `tests/service/crm/test_participant_stakeholder_service_boundaries.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path


def test_participant_vertical_module_exists() -> None:
    assert Path("src/service/crm/participants/__init__.py").exists()
    assert Path("src/service/crm/participants/contracts.py").exists()
    assert Path("src/service/crm/participants/participant_read_service.py").exists()
    assert Path("src/service/crm/participants/participant_write_service.py").exists()
    assert Path("src/service/crm/participants/stakeholder_read_service.py").exists()
    assert Path("src/service/crm/participants/stakeholder_write_service.py").exists()
    assert Path("src/service/crm/participants/stakeholder_adapter.py").exists()


def test_stakeholder_table_is_not_reintroduced() -> None:
    result = subprocess.run(
        ["rg", "-n", "class .*Stakeholder.*table=True|__tablename__ = \"deal_stakeholders\"",
         "src/domain/models", "migrations"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert "deal_stakeholders" not in result.stdout


def test_new_participant_services_do_not_commit() -> None:
    for path in [
        "src/service/crm/participants/participant_write_service.py",
        "src/service/crm/participants/stakeholder_write_service.py",
    ]:
        source = Path(path).read_text()
        assert ".commit(" not in source


def test_new_stakeholder_contracts_do_not_expose_stakeholder_id() -> None:
    source = Path("src/service/crm/participants/contracts.py").read_text()
    assert "stakeholder_id:" not in source
    assert "participant_id:" in source
```

- [ ] **Step 2: Run boundary tests and verify they fail**

Run:

```bash
uv run pytest tests/service/crm/test_participant_stakeholder_service_boundaries.py -q
```

Expected: FAIL because `src/service/crm/participants/` does not exist yet.

- [ ] **Step 3: Create package and contracts**

Create `src/service/crm/participants/__init__.py`:

```python
"""Participant-backed deal people services for CRM deals."""
```

Create `src/service/crm/participants/contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealParticipant

ParticipantChangedReason = Literal["created", "restored", "updated", "skipped"]
ParticipantMatchedBy = Literal["contact_id", "email", "company_id", "user_id", "created"]


@dataclass(frozen=True)
class ParticipantWriteContext:
    """Caller metadata for future participant audit events."""

    source: str
    actor_user_id: UUID
    source_label: str | None = None


@dataclass(frozen=True)
class ParticipantSkipped:
    """Participant request skipped by the canonical participant writer."""

    participant_type: str
    related_id: UUID
    relationship_role: str | None
    reason: str


@dataclass(frozen=True)
class ParticipantWriteResult:
    """Canonical result for raw participant membership writes."""

    created: list[CRMDealParticipant]
    skipped: list[ParticipantSkipped]
    contact_participant_changed: bool
    integration_id: UUID


@dataclass(frozen=True)
class StakeholderWriteResult:
    """Canonical result for stakeholder semantic writes."""

    participant: CRMDealParticipant
    created: bool
    matched_by: ParticipantMatchedBy


class ParticipantServiceError(Exception):
    """HTTP-shaped participant service error for routers and tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
```

## Task 1: Move Stakeholder Adapter Into Participant Module

**Files:**

- Create: `src/service/crm/participants/stakeholder_adapter.py`
- Modify: `src/service/crm/insights/participant_stakeholder_adapter.py`
- Test: `tests/service/crm/insights/test_participant_stakeholder_adapter.py`

- [ ] **Step 1: Copy adapter implementation**

Move the current implementation from:

```text
src/service/crm/insights/participant_stakeholder_adapter.py
```

to:

```text
src/service/crm/participants/stakeholder_adapter.py
```

- [ ] **Step 2: Keep old adapter as re-export**

Replace `src/service/crm/insights/participant_stakeholder_adapter.py` with:

```python
"""Compatibility re-export for participant-backed stakeholder adapter."""

from src.service.crm.participants.stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)

__all__ = ["participant_to_stakeholder_detail_dict"]
```

- [ ] **Step 3: Run adapter tests**

Run:

```bash
uv run pytest tests/service/crm/insights/test_participant_stakeholder_adapter.py -q
```

Expected: PASS.

## Task 2: Extract ParticipantWriteService

**Files:**

- Create: `src/service/crm/participants/participant_write_service.py`
- Modify: `src/service/crm/internal/deal_participant_service.py`
- Test: `tests/service/crm/test_deal_participant_service_internal_ids.py`

- [ ] **Step 1: Add or keep focused participant tests**

The tests must prove:

```python
async def test_add_participants_reuses_contact_when_same_contact_has_multiple_roles(...):
    ...

async def test_maybe_apply_company_to_deal_sets_internal_company_id(...):
    ...
```

These already exist in `tests/service/crm/test_deal_participant_service_internal_ids.py`.

- [ ] **Step 2: Create ParticipantWriteService**

Create `src/service/crm/participants/participant_write_service.py` by moving behavior from `DealParticipantService`:

```text
add_participants
remove_participant
update_participant_role
_get_internal_deal
_resolve_participant_entity
_find_existing_participant
_participant_identity_key
_parse_participant_type
_maybe_apply_company_to_deal
_refresh_deal_company_after_removal
```

The class constructor should be:

```python
class ParticipantWriteService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
        master_session: AsyncSession | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.master_session = master_session
```

Return `ParticipantWriteResult` from `add_participants()` while keeping the compatibility facade able to return the old tuple shape.

- [ ] **Step 3: Convert DealParticipantService to facade**

Keep `src/service/crm/internal/deal_participant_service.py` temporarily:

```python
class DealParticipantService:
    """Compatibility facade; use ParticipantWriteService for new code."""
```

Delegate:

```text
add_participants -> ParticipantWriteService.add_participants
remove_participant -> ParticipantWriteService.remove_participant
update_participant_role -> ParticipantWriteService.update_participant_role
```

Translate `ParticipantServiceError` to `DealParticipantServiceError` for old callers.

- [ ] **Step 4: Verify participant behavior**

Run:

```bash
uv run pytest tests/service/crm/test_deal_participant_service_internal_ids.py -q
```

Expected: PASS.

## Task 3: Extract StakeholderReadService

**Files:**

- Create: `src/service/crm/participants/stakeholder_read_service.py`
- Modify later: `src/service/crm/insights/stakeholders_service.py`
- Test: `tests/service/crm/insights/test_stakeholders_service_participant_reads.py`

- [ ] **Step 1: Create StakeholderReadService**

Create `src/service/crm/participants/stakeholder_read_service.py` from the read-only methods in `StakeholdersService`:

```text
list_stakeholders_by_deal
get_stakeholder
get_stakeholder_detail
list_available_contacts
_get_participant_contact
_participant_detail
_participant_timeline_stakeholder only if still needed by read DTOs
```

The constructor should be:

```python
class StakeholderReadService:
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

- [ ] **Step 2: Keep adapter import in new module**

Use:

```python
from src.service.crm.participants.stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)
```

- [ ] **Step 3: Verify stakeholder reads**

Run:

```bash
uv run pytest tests/service/crm/insights/test_stakeholders_service_participant_reads.py -q
```

Expected: PASS through compatibility facade initially.

## Task 4: Extract StakeholderWriteService

**Files:**

- Create: `src/service/crm/participants/stakeholder_write_service.py`
- Modify later: `src/service/crm/insights/stakeholders_service.py`
- Test: `tests/service/crm/insights/test_stakeholders_service_participant_writes.py`

- [ ] **Step 1: Create StakeholderWriteService**

Create `src/service/crm/participants/stakeholder_write_service.py` from stakeholder write methods in `StakeholdersService`:

```text
create_stakeholder
add_contacts_as_stakeholders
update_stakeholder
approve_stakeholder
reject_stakeholder
remove_stakeholder
_ensure_contact_for_stakeholder
_ensure_deal_participant
_find_contact_participant
_apply_contact_profile_updates
_apply_participant_behavior_updates
_soft_delete_internal_participant
_serialize_external_links
_get_internal_domains
_is_internal_email
```

Keep timeline event creation in this service because stakeholder semantic writes own stakeholder audit/timeline meaning.

- [ ] **Step 2: Remove service-owned commits**

Replace service commits with flush/refresh only:

```python
await self.session.flush()
```

The router, agent tool, or future workflow dispatcher must call:

```python
await service.session.commit()
```

after a successful write.

- [ ] **Step 3: Return stable stakeholder DTOs**

The public DTO must contain:

```text
participant_id
```

and must not contain:

```text
stakeholder_id
```

- [ ] **Step 4: Verify stakeholder writes**

Run:

```bash
uv run pytest tests/service/crm/insights/test_stakeholders_service_participant_writes.py -q
```

Expected: PASS after facade and tests are adjusted for router-owned commits.

## Task 5: Convert StakeholdersService To Compatibility Facade

**Files:**

- Modify: `src/service/crm/insights/stakeholders_service.py`
- Test: `tests/service/crm/insights/test_stakeholders_service_participant_reads.py`
- Test: `tests/service/crm/insights/test_stakeholders_service_participant_writes.py`

- [ ] **Step 1: Replace implementation with delegation**

Keep the class name for compatibility:

```python
class StakeholdersService:
    """Compatibility facade; use participant stakeholder services for new code."""
```

Construct:

```python
self._reader = StakeholderReadService(org_session, organisation_id, user_id)
self._writer = StakeholderWriteService(org_session, organisation_id, user_id)
```

Delegate read methods to `_reader` and write methods to `_writer`.

- [ ] **Step 2: Preserve error compatibility**

Keep:

```python
class StakeholdersServiceError(Exception):
    ...
```

Translate `ParticipantServiceError` into `StakeholdersServiceError`.

- [ ] **Step 3: Verify facade behavior**

Run:

```bash
uv run pytest tests/service/crm/insights/test_stakeholders_service_participant_reads.py tests/service/crm/insights/test_stakeholders_service_participant_writes.py -q
```

Expected: PASS.

## Task 6: Route Routers To New Services

**Files:**

- Modify: `src/api/routers/crm/deal_participants_handler.py`
- Modify: `src/api/routers/crm/stakeholders_handler.py`
- Test: `tests/api/routers/crm/test_stakeholders_handler.py`

- [ ] **Step 1: Route participants API to ParticipantWriteService**

Use:

```python
from src.service.crm.participants.participant_write_service import ParticipantWriteService
```

The router keeps:

```text
commit/rollback
post-commit stakeholder sync dispatch
response shaping
```

- [ ] **Step 2: Route stakeholders API to stakeholder read/write services**

Use:

```python
from src.service.crm.participants.stakeholder_read_service import StakeholderReadService
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService
```

Reads use `StakeholderReadService`.

Writes use `StakeholderWriteService`.

The router keeps:

```text
commit/rollback
StakeholderJobVisibilityService job creation
stakeholder research dispatch
```

- [ ] **Step 3: Verify stakeholder API**

Run:

```bash
uv run pytest tests/api/routers/crm/test_stakeholders_handler.py -q
```

Expected: PASS.

## Task 7: Route Agent Tools To New Services

**Files:**

- Modify: `src/agents/tools/crm/add_stakeholder.py`
- Modify: `src/agents/tools/crm/create_stakeholder_if_missing.py`
- Modify: `src/agents/tools/crm/upsert_stakeholder.py`
- Modify: `src/agents/tools/crm/list_stakeholders.py`
- Modify: `src/agents/tools/crm/get_stakeholder_details.py`
- Test: `tests/agents/tools/crm/test_create_stakeholder_if_missing.py`
- Test: `tests/agents/tools/crm/test_upsert_stakeholder.py`
- Test: `tests/service/agents/test_stakeholder_tools.py`

- [ ] **Step 1: Route read tools to StakeholderReadService**

Use `StakeholderReadService` in:

```text
list_stakeholders.py
get_stakeholder_details.py
```

- [ ] **Step 2: Route write tools to StakeholderWriteService**

Use `StakeholderWriteService` in:

```text
add_stakeholder.py
create_stakeholder_if_missing.py
upsert_stakeholder.py
```

- [ ] **Step 3: Preserve explicit versus silent post-create behavior**

Explicit user add paths:

```text
create visible processing jobs
dispatch stakeholder research
```

Silent SummaryAgent write-back:

```text
dispatch research only
do not create visible rematch/mapping jobs unless product explicitly changes this
```

- [ ] **Step 4: Verify agent tools**

Run:

```bash
uv run pytest tests/agents/tools/crm/test_create_stakeholder_if_missing.py tests/agents/tools/crm/test_upsert_stakeholder.py tests/service/agents/test_stakeholder_tools.py -q
```

Expected: PASS.

## Task 8: Focused Boundary Verification

**Files:**

- No code files unless verification exposes a bug.

- [ ] **Step 1: Verify no legacy stakeholder table returns**

Run:

```bash
uv run pytest tests/service/crm/test_participant_stakeholder_service_boundaries.py -q
```

Expected: PASS.

- [ ] **Step 2: Verify participant write behavior**

Run:

```bash
uv run pytest tests/service/crm/test_deal_participant_service_internal_ids.py tests/service/crm/test_contact_deal_link_service.py -q
```

Expected: PASS.

- [ ] **Step 3: Verify stakeholder service behavior**

Run:

```bash
uv run pytest tests/service/crm/insights/test_stakeholders_service_participant_reads.py tests/service/crm/insights/test_stakeholders_service_participant_writes.py tests/service/crm/insights/test_participant_stakeholder_adapter.py -q
```

Expected: PASS.

- [ ] **Step 4: Verify API and agent surfaces**

Run:

```bash
uv run pytest tests/api/routers/crm/test_stakeholders_handler.py tests/agents/tools/crm/test_create_stakeholder_if_missing.py tests/agents/tools/crm/test_upsert_stakeholder.py tests/service/agents/test_stakeholder_tools.py -q
```

Expected: PASS.

- [ ] **Step 5: Run focused lint and type checks before PR**

Run:

```bash
uv run ruff check src/service/crm/participants src/service/crm/internal/deal_participant_service.py src/service/crm/insights/stakeholders_service.py src/api/routers/crm/deal_participants_handler.py src/api/routers/crm/stakeholders_handler.py src/agents/tools/crm/add_stakeholder.py src/agents/tools/crm/create_stakeholder_if_missing.py src/agents/tools/crm/upsert_stakeholder.py src/agents/tools/crm/list_stakeholders.py src/agents/tools/crm/get_stakeholder_details.py tests/service/crm/test_participant_stakeholder_service_boundaries.py
```

Expected: PASS.

Run:

```bash
uv run mypy --no-incremental src/service/crm/participants src/service/crm/internal/deal_participant_service.py src/service/crm/insights/stakeholders_service.py src/api/routers/crm/deal_participants_handler.py src/api/routers/crm/stakeholders_handler.py src/agents/tools/crm/add_stakeholder.py src/agents/tools/crm/create_stakeholder_if_missing.py src/agents/tools/crm/upsert_stakeholder.py src/agents/tools/crm/list_stakeholders.py src/agents/tools/crm/get_stakeholder_details.py
```

Expected: PASS.

## Later Cleanup Phase

Do this only after the new module is merged and production imports are migrated.

1. Search old imports:

   ```bash
   rg -n "DealParticipantService|StakeholdersService|participant_stakeholder_adapter" src tests
   ```

2. If only compatibility tests remain, move or delete:

   ```text
   src/service/crm/internal/deal_participant_service.py
   src/service/crm/insights/stakeholders_service.py
   src/service/crm/insights/participant_stakeholder_adapter.py
   ```

3. Review worker/job naming separately:

   ```text
   DealJobs.stakeholder_id
   dispatch_stakeholder_research_async(stakeholder_id=...)
   stakeholder.sync_deal_stakeholders
   ```

   Keep database columns if renaming creates unnecessary migration risk. Add comments/tests that these fields contain `participant_id`.

4. Review provider sync participant writers later:

   ```text
   src/service/integrations/crm/records/*/deals/participant_writer.py
   ```

   Do not route provider ingestion through the internal participant writer until we explicitly design provider-write semantics.

## Rollout Notes

- This plan combines participants and stakeholders at the service-module level, not by changing tables.
- The table is already unified: `crm_deal_participants`.
- The API can keep stakeholder routes because stakeholder is product language.
- `participant_id` remains the public identifier.
- Old service files stay temporarily as facades to avoid a risky big-bang refactor.
- Workflow dispatcher integration is not part of this slice.
- CRM mutation events for participant/stakeholder writes are not part of this slice.
- Account/contact services should remain the source for durable account/contact writes.

## Success Criteria

- `src/service/crm/participants/` exists with participant and stakeholder services.
- No new `deal_stakeholders` table/model/migration is introduced.
- Raw participant writes use `ParticipantWriteService`.
- Stakeholder reads use `StakeholderReadService`.
- Stakeholder semantic writes use `StakeholderWriteService`.
- Existing stakeholder APIs still return `participant_id`.
- New service files do not commit transactions directly.
- Visible job creation and Celery dispatch remain at router/tool/workflow orchestration boundaries.
- Compatibility facades keep existing imports working during rollout.
- Focused participant, stakeholder, API, agent, ruff, and mypy checks pass before PR.
