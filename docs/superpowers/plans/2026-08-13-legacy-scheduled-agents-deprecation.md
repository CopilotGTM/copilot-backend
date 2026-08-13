# Legacy Scheduled Agents Deprecation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the reminder-backed scheduled-agent feature from live backend and frontend surfaces while preserving Agent Studio and static reminders.

**Architecture:** The legacy feature is the `/users/agents` FastAPI router and the frontend `/agents` page, both built on the reminders implementation. Archive only the two main entry-point files under each repository's existing or new `deprecated/` tree, remove all live registrations and legacy-only client branches, and leave the shared reminder execution domain intact because internal tools and workers still depend on agentic reminders.

**Tech Stack:** Python 3.11, FastAPI, pytest, Next.js 16 App Router, React 19, TypeScript, Jest, Playwright

---

### Task 1: Remove the legacy backend API surface

**Files:**
- Move: `src/api/routers/reminders/agents_handler.py` to `deprecated/api/routers/reminders/agents_handler.py`
- Modify: `src/api/routers/reminders/__init__.py`
- Modify: `src/api/fastapi_app.py`
- Modify: `src/contracts/reminders.py`
- Modify: `src/api/schemas/reminder_models.py`
- Delete: `tests/api/routers/reminders/test_agents_handler.py`
- Modify: `tests/api/routers/test_external_api_versioning.py`

- [x] **Step 1: Change the route contract test to reject the legacy path**

  Keep the Agent Studio assertions and replace the old compatibility assertion with:

  ```python
  assert "/users/agents" not in paths
  ```

- [x] **Step 2: Run the route test and verify it fails before removal**

  Run: `uv run pytest tests/api/routers/test_external_api_versioning.py::test_user_agent_definition_routes_are_versioned -q`

  Expected: failure because `/users/agents` is still registered.

- [x] **Step 3: Archive the main legacy handler and remove live registration**

  Move the handler to `deprecated/api/routers/reminders/agents_handler.py`, then leave the reminders package exporting only:

  ```python
  from .reminders_handler import reminders_router

  __all__ = ["reminders_router"]
  ```

  In `src/api/fastapi_app.py`, import only `reminders_router` from the reminders package and delete:

  ```python
  fastapp.include_router(agents_router, prefix="/users/agents", tags=["agents"])
  ```

- [x] **Step 4: Delete legacy-only request schemas and tests**

  Remove `AgentCreateRequest` and `AgentUpdateRequest` from `src/contracts/reminders.py` and their re-exports from `src/api/schemas/reminder_models.py`. Delete `tests/api/routers/reminders/test_agents_handler.py`; do not alter the Agent Studio schemas in `src/api/schemas/agent_models.py`.

- [x] **Step 5: Verify backend routing and reminders**

  Run:

  ```powershell
  uv run pytest tests/api/routers/test_external_api_versioning.py tests/api/routers/reminders/test_reminders_handler.py -q
  ```

  Expected: both suites pass; OpenAPI contains `/users/v1/agents` and `/users/reminders`, but not `/users/agents`.

### Task 2: Remove the legacy frontend surface

**Files:**
- Move: `src/app/(root)/(dashboard)/agents/page.tsx` to `deprecated/legacy-scheduled-agents/page.tsx`
- Delete: `src/app/(root)/(dashboard)/agents/loading.tsx`
- Modify: `src/components/layout/sidebar/sidebar.tsx`
- Modify: `src/app/(root)/(dashboard)/reminders/page.tsx`
- Modify: `src/components/reminders/ReminderListPage.tsx`
- Modify: `src/components/reminders/CreateEditReminderDrawer.tsx`
- Modify: `src/components/reminders/ReminderItem.tsx`
- Modify: `src/components/reminders/ReminderExpandedPanel.tsx`
- Modify: `src/components/reminders/ReminderHistory.tsx`
- Modify: `src/modules/reminders/api/reminders.client.ts`
- Modify: `src/modules/reminders/api/reminders.api.ts`
- Modify: `src/modules/reminders/hooks/use-reminders.ts`
- Modify: `src/modules/reminders/hooks/use-reminder-actions.ts`
- Modify: `src/modules/reminders/types/reminders.types.ts`
- Modify: `src/modules/reminders/constants/reminders.constants.ts`
- Modify: `__tests__/unit/components/reminders/reminder-list-page.test.tsx`
- Delete: `__tests__/unit/components/reminders/create-edit-reminder-drawer.test.tsx`
- Modify: `__tests__/unit/modules/reminders/reminders.api.test.ts`
- Delete: `__tests__/e2e/reminders-agents-live.spec.ts`
- Delete: `__tests__/e2e/agents-create-modal.spec.ts`

- [x] **Step 1: Update the route and navigation checks**

  Delete the mixed live E2E because it contains legacy-agent coverage and mutates authenticated reminder data during cleanup. Use these source-contract commands after the route move:

  ```powershell
  rg -n 'href: "/agents"|/api/proxy/agents' src
  rg -n 'href: "/agents-v1"' src/components/layout/sidebar/sidebar.tsx
  ```

  Expected: the first command returns no matches and the second returns the Agent Studio sidebar item.

- [x] **Step 2: Archive the main page and remove App Router registration**

  Move only the legacy `page.tsx` to `deprecated/legacy-scheduled-agents/page.tsx` and delete its route-local loading file. Because the archived file is outside `src/app`, Next.js no longer registers `/agents`.

- [x] **Step 3: Remove legacy navigation**

  Delete this sidebar item and its now-unused icon import:

  ```tsx
  { name: "Agents", icon: Bot, href: "/agents" },
  ```

  Preserve the conditional Agent Studio item at `/agents-v1`.

- [x] **Step 4: Collapse the reminders client and UI to static reminders**

  Remove the `/api/proxy/agents` Axios client, `clientForType`, agentic request unions, agent-only labels, and `productKind`/`fixedReminderType` branches. All live reminder API calls must use `remindersApiClient`; the create payload must remain:

  ```ts
  {
    message_content: form.message_content.trim(),
    ...scheduleAndDeliveryFields
  }
  ```

  Keep the response shape required by static reminders, but remove user controls and client paths that create or manage scheduled agents.

- [x] **Step 5: Remove legacy-only tests and update reminder coverage**

  Delete the dedicated legacy agent E2E spec and the mixed destructive live spec, remove agent-mode cases from shared reminder tests, and keep static reminder coverage in the list, API, sidebar, and backend handler suites.

- [x] **Step 6: Verify frontend**

  Run:

  ```powershell
  yarn test:jest --runInBand __tests__/unit/components/main-sidebar.test.tsx __tests__/unit/components/reminders/reminder-list-page.test.tsx __tests__/unit/modules/reminders/reminders.api.test.ts
  yarn check-types
  ```

  Expected: focused Jest suites and TypeScript checks pass; source search finds no `/api/proxy/agents` or live `/agents` route outside the deprecated snapshot.

### Task 3: Audit the final boundary

**Files:**
- Inspect: both repository diffs and Git status

- [x] **Step 1: Search for forbidden live references**

  Run focused `rg` checks for `/users/agents`, `/api/proxy/agents`, `href: "/agents"`, and the removed route files. References are allowed only in the archived snapshots and the backend route-absence regression test.

- [x] **Step 2: Confirm protected surfaces remain**

  Confirm current source still registers `/users/reminders`, `/users/v1/agents`, and the frontend `/agents-v1` routes and sidebar item.

- [x] **Step 3: Review diffs without staging unrelated work**

  Run `git diff --check`, `git diff --stat`, and `git status --short` in both repositories. Preserve the backend's pre-existing staged planning documents and leave all new deprecation changes unstaged.

### Task 4: Remove scheduled-agent creation from backend tools

**Files:**
- Modify: `src/agents/tools/reminders/create_reminder.py`
- Modify: `src/agents/tools/reminders/list_reminders.py`
- Modify: `src/agents/tools/reminders/update_reminder.py`
- Modify: `src/agents/tools/reminders/cancel_reminder.py`
- Modify: `src/agents/tools/registry.py`
- Modify: `src/service/voice_realtime/tool_manifest.py`
- Modify: `src/contracts/reminders.py`
- Modify: `src/api/schemas/reminder_models.py`
- Modify: `tests/agents/tools/reminders/test_create_reminder.py`
- Modify: `tests/agents/tools/test_update_note_task_reminder_tools.py`
- Modify: `tests/service/voice_realtime/test_tool_manifest.py`
- Modify: `tests/api/routers/reminders/test_reminders_handler.py`

- [x] **Step 1: Prove the chat and voice tool contracts still expose agent creation**

  Add failing assertions that `reminder_type`, `agent_prompt`, and `deal_id` are absent from both tool signatures and the voice schema.

- [x] **Step 2: Collapse reminder creation to static reminders**

  Remove the three scheduled-agent fields from the registered tool, always create `ReminderType.STATIC`, and delete the unused preview-form implementation that duplicated agentic creation.

- [x] **Step 3: Delete dead generic agentic write contracts**

  Remove `ReminderCreateRequest` and `ReminderUpdateRequest`; live reminder routes already use their static-only counterparts.

- [x] **Step 4: Restrict sibling reminder tools to static records**

  Filter list, update, and cancel operations to `ReminderType.STATIC` and remove the agent-prompt update parameter so existing legacy rows are not exposed through chat tools.

- [x] **Step 5: Verify protected surfaces**

  Run the reminder tool, voice manifest, reminder API, Agent Studio contract/catalog, Ruff, comment, source-audit, and whitespace checks. Keep internal execution support for already-persisted agentic reminders; it is not registered as a creation surface.
