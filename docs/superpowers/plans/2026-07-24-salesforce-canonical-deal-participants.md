# Salesforce Canonical Deal Participants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist one active participant per Salesforce deal/contact while combining standard contact-role facts with custom Contact lookup facts and preserving soft-deleted history.

**Architecture:** Normalize repeated standard `OpportunityContactRole` rows by `(deal, participant type, contact)` before persistence, merging primary and bounded stakeholder facts into one canonical input. Keep the existing writer as the cross-phase upsert boundary for standard roles and custom Contact lookup fields. When a stale source mapping points at a deleted participant but the same deal/contact already has an active participant, update the active participant and repoint the mappings instead of reactivating the stale row. The existing database constraint remains unchanged.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy, PostgreSQL, Alembic, pytest.

---

### Task 1: Canonicalize standard Salesforce contact-role inputs

**Files:**
- Modify: `src/service/integrations/crm/records/salesforce/deals/participant_normalizer.py`
- Test: `tests/service/integrations/crm/records/salesforce/deals/test_participant_normalizer.py`

- [x] **Step 1: Write the failing test**

Add a test that supplies two `OpportunityContactRole` rows for the same opportunity/contact: one carries `IsPrimary=true`, and the other carries `Role="Decision Maker"`. Assert that normalization returns exactly one contact participant with both `is_primary=True` and `relationship_role="Decision Maker"`.

- [x] **Step 2: Run the test to verify RED**

Run:

```powershell
uv run pytest tests/service/integrations/crm/records/salesforce/deals/test_participant_normalizer.py -k "same_contact" -q
```

Expected: failure because `_dedup_participants` currently includes `relationship_role` in identity and returns two participants.

- [x] **Step 3: Implement the minimum canonical merge**

Change `_dedup_participants` to key rows by deal, participant type, and related external ID. Merge repeated rows by preserving a non-empty business role, OR-ing `is_primary`, and filling bounded `decision_power`/`stance` only when the canonical value is unknown.

- [x] **Step 4: Run the participant-normalizer tests**

Run:

```powershell
uv run pytest tests/service/integrations/crm/records/salesforce/deals/test_participant_normalizer.py -q
```

Expected: all tests pass.

### Task 2: Prefer the active canonical participant over a stale mapped tombstone

**Files:**
- Modify: `src/service/integrations/crm/records/salesforce/deals/participant_writer.py`
- Test: `tests/service/integrations/crm/records/salesforce/deals/test_deal_child_source_mappings.py`

- [x] **Step 1: Write the failing writer regression test**

Create a deleted participant selected by the Salesforce source mapping and an active participant with the same deal/contact identity. Assert that the writer updates the active participant, leaves the tombstone deleted, and points all source mappings at the active participant.

- [x] **Step 2: Run the test to verify RED**

Run:

```powershell
uv run pytest tests/service/integrations/crm/records/salesforce/deals/test_deal_child_source_mappings.py -k "stale_mapping" -q
```

Expected: failure because the source-mapped tombstone currently wins over the active identity row.

- [x] **Step 3: Fix canonical target selection**

Resolve the active identity row first when the related contact is known, using the mapped row only as a fallback. This reuses the current identity lookup and source-mapping upsert; it does not add a new abstraction or migration.

- [x] **Step 4: Run participant-writer tests**

Run:

```powershell
uv run pytest tests/service/integrations/crm/records/salesforce/deals/test_deal_child_source_mappings.py -q
```

Expected: all tests pass.

### Task 3: Verify the complete stakeholder path

**Files:**
- Verify only: Salesforce deal participant and stakeholder tests.

- [x] **Step 1: Run focused Salesforce deal tests**

```powershell
uv run pytest tests/service/integrations/crm/records/salesforce/deals -q
```

- [x] **Step 2: Run stakeholder read/write tests**

```powershell
uv run pytest tests/service/crm/insights/test_stakeholders_service_participant_writes.py tests/workers/deal_worker/services/test_stakeholder_lookup_service.py -q
```

- [x] **Step 3: Run lint and inspect the final diff**

```powershell
uv run ruff check src/service/integrations/crm/records/salesforce/deals/participant_normalizer.py src/service/integrations/crm/records/salesforce/deals/participant_writer.py tests/service/integrations/crm/records/salesforce/deals/test_participant_normalizer.py tests/service/integrations/crm/records/salesforce/deals/test_deal_child_source_mappings.py
git diff --check
git diff --stat
```

Expected: all checks pass; changes are limited to the plan, canonical normalization, canonical writer selection, and their tests.
