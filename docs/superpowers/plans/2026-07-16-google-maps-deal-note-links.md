# Google Maps Deal Note Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve a human-readable, clickable Google Maps location when a user adds a deal note from a shared WhatsApp location or map link.

**Architecture:** Keep note persistence unchanged because it already stores supplied text verbatim. Enrich WhatsApp's normalized location context with a canonical Google Maps URL, then teach the shared CRM mutation skill to retain the original or canonical map link, location name, and address in note bodies instead of reducing the location to coordinates.

**Tech Stack:** Python, pytest, WhatsApp webhook normalization, Agno local skills.

---

### Task 1: Lock the location-note contract

**Files:**
- Modify: `tests/service/bots/test_whatsapp_bot_service.py`
- Modify: `tests/agents/test_skills_loading.py`

- [ ] **Step 1: Add a failing WhatsApp normalization assertion**

Extend `test_extract_inbound_location_as_text_context` to require:

```python
assert "Google Maps: https://www.google.com/maps/search/?api=1&query=12.9716%2C77.5946" in messages[0].text
```

- [ ] **Step 2: Add a failing CRM skill assertion**

Add a focused skill test requiring the instructions to preserve a Google Maps link, location name, and address and to forbid replacing the link with coordinates alone.

- [ ] **Step 3: Run the two tests and confirm they fail**

Run:

```powershell
uv run pytest tests/service/bots/test_whatsapp_bot_service.py::test_extract_inbound_location_as_text_context tests/agents/test_skills_loading.py::test_crm_change_workflow_skill_preserves_map_links_in_notes -q
```

Expected: both tests fail because neither the canonical URL nor the note rule exists.

### Task 2: Preserve clickable map context

**Files:**
- Modify: `src/service/bots/whatsapp_bot_service.py`
- Modify: `src/agents/skills/crm-change-workflow/SKILL.md`

- [ ] **Step 1: Add the canonical map URL to WhatsApp location text**

Build `https://www.google.com/maps/search/?api=1&query=<latitude>%2C<longitude>` from Meta's validated coordinates and append it as `Google Maps: <url>`. Keep the existing latitude, longitude, name, and address lines for grounding.

- [ ] **Step 2: Add the shared deal-note rule**

In the CRM skill, require `AddDealNote` and `update_deal_note` payloads sourced from a map URL or shared location to preserve:

- the original Google Maps URL when present;
- the canonical URL supplied by channel normalization when only coordinates were received;
- any supplied location name and address;
- coordinates as supporting detail, never as the only representation.

- [ ] **Step 3: Run focused tests**

Run the two tests from Task 1 and expect both to pass.

### Task 3: Verify the bounded change

**Files:**
- Verify: `src/service/bots/whatsapp_bot_service.py`
- Verify: `src/agents/skills/crm-change-workflow/SKILL.md`
- Verify: `tests/service/bots/test_whatsapp_bot_service.py`
- Verify: `tests/agents/test_skills_loading.py`

- [ ] **Step 1: Run relevant suites**

```powershell
uv run pytest tests/service/bots/test_whatsapp_bot_service.py tests/agents/test_skills_loading.py -q
```

- [ ] **Step 2: Run quality checks**

```powershell
uv run ruff check src/service/bots/whatsapp_bot_service.py tests/service/bots/test_whatsapp_bot_service.py tests/agents/test_skills_loading.py
uv run ruff format --check src/service/bots/whatsapp_bot_service.py tests/service/bots/test_whatsapp_bot_service.py tests/agents/test_skills_loading.py
uv run python .agents/skills/writing-python-comments/scripts/check_comment_smells.py src/service/bots/whatsapp_bot_service.py
git diff --check -- src/service/bots/whatsapp_bot_service.py src/agents/skills/crm-change-workflow/SKILL.md tests/service/bots/test_whatsapp_bot_service.py tests/agents/test_skills_loading.py
```

- [ ] **Step 3: Confirm scope**

Verify that note persistence and frontend rendering were not changed. The backend guarantees a clickable URL in stored note text; richer map-card rendering remains a separate frontend capability.
