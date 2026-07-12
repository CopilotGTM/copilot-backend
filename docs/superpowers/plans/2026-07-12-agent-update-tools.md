# Agent Update Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the GeneralAgent update internal deal notes, CRM tasks, and static or agentic reminders from every supported chat channel.

**Architecture:** Add three thin agent-tool adapters over the existing `NotesWriteService`, `TaskWriteService`, and `ReminderService`. Keep tenant and user identity immutable in `AgentContext`, require resolved record IDs, reuse existing date and assignee helpers, and register the tools centrally.

**Tech Stack:** Python 3.12, Agno callable tools, SQLAlchemy async sessions, pytest, Ruff.

---

### Task 1: Lock contracts with failing tests

**Files:**
- Create: `tests/agents/tools/crm/test_update_deal_note.py`
- Create: `tests/agents/tools/tasks/test_update_task.py`
- Create: `tests/agents/tools/reminders/test_update_reminder.py`
- Create: `tests/agents/tools/test_registry_update_tools.py`

- [ ] Test ID validation, empty-update rejection, service payloads, transactions, results, reminder type detection, date parsing, and channel registration.
- [ ] Run focused tests and verify they fail because the tools do not exist.

### Task 2: Add minimum adapters

**Files:**
- Create: `src/agents/tools/crm/update_deal_note.py`
- Create: `src/agents/tools/tasks/update_task.py`
- Create: `src/agents/tools/reminders/update_reminder.py`
- Modify: the three package `__init__.py` files.

- [ ] Implement note updates through `NotesWriteService.update_user_note`.
- [ ] Implement task updates through `TaskWriteService.update_task`, reusing create-task date and assignee resolution.
- [ ] Implement type-aware reminder updates through `ReminderService.get` and `ReminderService.update`, reusing reminder date parsing.
- [ ] Export factories and run focused tests to green.

### Task 3: Register and teach GeneralAgent

**Files:**
- Modify: `src/agents/tools/registry.py`
- Modify: `src/agents/skills/crm-change-workflow/SKILL.md`
- Modify: `src/agents/skills/reminders/SKILL.md`

- [ ] Register names, categories, factories, GeneralAgent mappings, and Teams, Slack, WhatsApp, web, and voice channels.
- [ ] Document list/search-before-update resolution, no guessed IDs, and only-explicit-field mutation.
- [ ] Run registry and skill tests.

### Task 4: Verify

- [ ] Run new and closely related tests, Ruff on changed Python files, `git diff --check`, and final scope review.
