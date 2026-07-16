# Chat Deal Update Targeting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let web and messaging agents update a resolved deal by passing its ID, using the same search-first contract as deal notes.

**Architecture:** Keep `organisation_id` as the immutable tenant boundary and make `deal_id` an explicit tool input. Reuse `DealWriteService` for every mutation; do not introduce session-level active-deal state or a parallel writer.

**Tech Stack:** Python, Agno callable tools, Pydantic validation, pytest.

---

### Task 1: Lock the explicit-deal tool contract

**Files:**
- Modify: `tests/agents/tools/crm/test_deal_update_tools.py`

- [ ] Add tests that build `AgentContext` without `deal_id`, pass a resolved `deal_id` into each update tool, and assert the shared writer receives that ID.
- [ ] Add invalid-ID coverage proving malformed user/tool input fails before opening a database session.
- [ ] Run the new tests and confirm they fail because the current callables do not accept `deal_id`.

### Task 2: Make all deal-update tools organization-scoped

**Files:**
- Modify: `src/agents/tools/crm/update_deal_fields.py`
- Modify: `src/agents/tools/crm/update_deal_account.py`
- Modify: `src/agents/tools/crm/update_deal_lifecycle.py`

- [ ] Add required `deal_id: str` tool arguments and parse them as UUIDs.
- [ ] Keep `organisation_id` sourced only from `AgentContext` and route writes through `DealWriteService`.
- [ ] Document the same `search_deals -> disambiguate -> update` workflow used by deal notes.
- [ ] Run the focused regression file until green.

### Task 3: Align the CRM change skill

**Files:**
- Modify: `src/agents/skills/crm-change-workflow/SKILL.md`

- [ ] Make deal-update routing explicitly require `search_deals`, one resolved match, and the returned `deal_id`.
- [ ] Add the create-then-rename dependency example so the model reuses the created ID.

### Task 4: Verify and review

**Files:**
- Check: all files above

- [ ] Run focused pytest for deal-update tools and registry contracts.
- [ ] Run Ruff on changed Python files and the Python comment-smell checker.
- [ ] Inspect the scoped diff for unrelated edits.
- [ ] Request an independent code review and resolve all Critical or Important findings.
- [ ] Re-run the focused verification after review fixes.
