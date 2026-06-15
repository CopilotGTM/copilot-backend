# Stakeholder Tool Surface Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the remaining legacy `stakeholder_id` assumptions from agent stakeholder tools and make each stakeholder create path use the correct post-create behavior.

**Architecture:** `CRMDealParticipant.participant_id` is the canonical stakeholder identifier. Explicit user-created stakeholders should create visible deal processing jobs and stakeholder research. Silent SummaryAgent write-back should stay minimal and should not start user-visible email/calendar rematch jobs unless product explicitly wants background meeting-summary writes to show in deal progress.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy async sessions, Celery deal-worker, pytest, ruff.

---

## Current Findings

The participant-backed rollout moved canonical stakeholder identity from legacy stakeholder rows to `CRMDealParticipant.participant_id`.

Confirmed active write paths:

| Path | Entry point | Current intent | Required post-create behavior |
| --- | --- | --- | --- |
| Manual create API | `src/api/routers/crm/stakeholders_handler.py::create_stakeholder` | User explicitly adds a new stakeholder | Visible processing jobs + research |
| Add contacts API | `src/api/routers/crm/stakeholders_handler.py::add_contacts_as_stakeholders` | User explicitly adds existing contacts | Visible processing jobs + research for each newly added participant |
| Create deal inline contacts | `src/api/routers/crm/deals_handler.py::create_deal` | User creates a deal with selected contacts | Visible processing jobs + research for each newly added participant |
| Agent direct add | `src/agents/tools/crm/add_stakeholder.py::add_stakeholder_direct` | User asks agent/voice/chat to add stakeholder | Visible processing jobs + research |
| Teams/Slack card submit | `src/service/bots/form_handlers/add_stakeholder_handler.py` | User submits explicit add-stakeholder card | Visible processing jobs + research |
| SummaryAgent silent create | `src/agents/tools/crm/create_stakeholder_if_missing.py` | Meeting summary creates materially discussed external people | Research only, unless product changes this contract |
| Registered but inactive upsert | `src/agents/tools/crm/upsert_stakeholder.py` | Create-or-update by email, not currently exposed in tested agent tool sets | Keep inactive or remove; if kept, fix participant-id-only DTO handling |

Important deployment note: all queued mapping/research work depends on the `copilot-backend-deal-worker` deployment being scaled up. A code fix can create jobs, but `deal-worker = 0/0` means those jobs will not execute.

## Problems To Fix

1. `create_stakeholder_if_missing.py` still references `new_stakeholder.stakeholder_id`.
2. `upsert_stakeholder.py` still references `new_stakeholder.stakeholder_id`.
3. `_format_upsert_output()` uses `getattr(...participant_id...) or stakeholder.stakeholder_id`, which can eagerly fail for DTOs that only expose `participant_id`.
4. The plan must not imply that SummaryAgent silent write-back should create visible user-facing mapping jobs. That is a product decision, not a migration cleanup.
5. `UPSERT_STAKEHOLDER` has a registry factory, but tests already assert it should not be exposed to active agents. The cleanup should either remove the dead factory surface or keep it fixed and inactive.

## Non-Goals

- Do not recreate `deal_stakeholders` behavior.
- Do not reintroduce public `stakeholder_id` response fields.
- Do not add visible mapping jobs to SummaryAgent silent write-back unless product explicitly approves that behavior.
- Do not change frontend progress behavior beyond ensuring explicit add paths create the existing `DealJobs` rows.

---

## Task 1: Fix Participant-Only DTO Handling In Silent Create Tools

**Files:**
- Modify: `src/agents/tools/crm/create_stakeholder_if_missing.py`
- Modify: `src/agents/tools/crm/upsert_stakeholder.py`
- Test: `tests/agents/tools/crm/test_create_stakeholder_if_missing.py`
- Test: `tests/agents/tools/crm/test_upsert_stakeholder.py`

- [ ] **Step 1: Add failing tests for participant-only create DTOs**

Add a test where the fake `StakeholdersService.create_stakeholder()` returns an object with `participant_id` and no `stakeholder_id`.

Expected assertions:

```python
dispatch_stakeholder_research_async.assert_awaited_once_with(
    organisation_id=organisation_id,
    stakeholder_id=participant_id,
    deal_id=deal_id,
)
```

For `upsert_stakeholder`, also assert the tool result contains:

```python
assert result["participant_id"] == str(participant_id)
assert "stakeholder_id" not in result
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run:

```bash
uv run pytest tests/agents/tools/crm/test_create_stakeholder_if_missing.py tests/agents/tools/crm/test_upsert_stakeholder.py -q
```

Expected before implementation: failure caused by `AttributeError: ... stakeholder_id`.

- [ ] **Step 3: Replace stale `stakeholder_id` usage**

In `src/agents/tools/crm/create_stakeholder_if_missing.py`, replace research dispatch and logging references:

```python
await dispatch_stakeholder_research_async(
    organisation_id=organisation_id,
    stakeholder_id=new_stakeholder.participant_id,
    deal_id=actual_deal_id,
)
```

In `src/agents/tools/crm/upsert_stakeholder.py`, replace create-path research dispatch and logging references:

```python
await dispatch_stakeholder_research_async(
    organisation_id=organisation_id,
    stakeholder_id=new_stakeholder.participant_id,
    deal_id=actual_deal_id,
)
```

- [ ] **Step 4: Make formatting participant-first without eager legacy access**

Change `_format_upsert_output()` from fallback attribute access that can raise to explicit safe lookup:

```python
participant_id = getattr(stakeholder, "participant_id", None)
if participant_id is None:
    participant_id = getattr(stakeholder, "stakeholder_id", None)
```

Then return only the public participant key:

```python
"participant_id": str(participant_id) if participant_id else None,
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/agents/tools/crm/test_create_stakeholder_if_missing.py tests/agents/tools/crm/test_upsert_stakeholder.py -q
```

Expected: all selected tests pass.

---

## Task 2: Lock Active Agent Tool Exposure

**Files:**
- Modify if needed: `src/agents/tools/registry.py`
- Test: `tests/service/agents/test_stakeholder_tools.py`
- Test: `tests/service/voice_realtime/test_tool_manifest.py`

- [ ] **Step 1: Verify the current active exposure**

Run:

```bash
uv run pytest tests/service/agents/test_stakeholder_tools.py tests/service/voice_realtime/test_tool_manifest.py -q
```

Expected current contract:

```text
GeneralAgent exposes add_stakeholder.
VoiceRealtimeAgent exposes add_stakeholder.
SummaryAgent exposes create_stakeholder_if_missing.
UPSERT_STAKEHOLDER is not exposed to active agent tool sets.
```

- [ ] **Step 2: Decide whether to remove the inactive upsert factory**

If no active workflow needs `UPSERT_STAKEHOLDER`, remove the enum/factory/import surface from:

```text
src/agents/tools/registry.py
src/agents/tools/crm/__init__.py
```

Keep `src/agents/tools/crm/upsert_stakeholder.py` only if existing tests or future workflow plans still exercise it directly.

- [ ] **Step 3: Keep SummaryAgent behavior explicit**

Do not add `StakeholderJobVisibilityService` to `create_stakeholder_if_missing.py` in this cleanup. Its current contract is silent meeting-summary write-back. It should dispatch research only.

- [ ] **Step 4: Run exposure tests again**

Run:

```bash
uv run pytest tests/service/agents/test_stakeholder_tools.py tests/service/voice_realtime/test_tool_manifest.py -q
```

Expected: exposure contract remains green.

---

## Task 3: Verify Explicit Create Paths Trigger Jobs

**Files:**
- Test: `tests/api/routers/crm/test_deals_handler.py`
- Test: `tests/api/routers/crm/test_stakeholders_handler.py`
- Test: `tests/agents/tools/crm/test_add_stakeholder_direct.py`
- Test: `tests/service/bots/form_handlers/test_add_stakeholder_handler.py`

- [ ] **Step 1: Check direct API create paths**

Run:

```bash
uv run pytest tests/api/routers/crm/test_stakeholders_handler.py tests/api/routers/crm/test_deals_handler.py::test_create_deal_adds_selected_contacts_through_stakeholder_service -q
```

Expected:

```text
create_stakeholder creates visible processing jobs and research.
add-from-contacts creates visible processing jobs and research.
create-deal inline contacts creates visible processing jobs and research.
```

- [ ] **Step 2: Check agent and card submit paths**

Run:

```bash
uv run pytest tests/agents/tools/crm/test_add_stakeholder_direct.py tests/service/bots/form_handlers/test_add_stakeholder_handler.py -q
```

Expected:

```text
add_stakeholder_direct creates visible processing jobs and research.
add_stakeholder_submit creates visible processing jobs and research.
```

- [ ] **Step 3: Run lint on changed files**

Run:

```bash
uv run ruff check src/agents/tools/crm/create_stakeholder_if_missing.py src/agents/tools/crm/upsert_stakeholder.py tests/agents/tools/crm/test_create_stakeholder_if_missing.py tests/agents/tools/crm/test_upsert_stakeholder.py
```

Expected: `All checks passed!`

---

## Final Acceptance Criteria

- No create path accesses `new_stakeholder.stakeholder_id`.
- Public/tool payloads expose `participant_id`, not `stakeholder_id`.
- Explicit user add paths create `StakeholderJobVisibilityService` jobs and dispatch research.
- SummaryAgent silent create path dispatches research using `participant_id` and does not create visible mapping jobs.
- `UPSERT_STAKEHOLDER` is either removed from the active factory surface or remains fixed and inactive.
- Focused pytest and ruff checks listed above pass.
