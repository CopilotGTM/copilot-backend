# Business-Friendly Trigger Filters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Agent Studio trigger understandable to a business user by showing automatic execution scope explicitly, replacing raw identifiers with searchable business labels, and hiding technical event metadata that users cannot reasonably know.

**Architecture:** Keep the backend trigger catalog authoritative. Execution authority remains an immutable runtime scope, while optional filters only narrow that scope; the frontend must never manufacture authority by silently inserting a filter. Extend catalog field presentation metadata and tenant-scoped option sources, reuse existing CRM and integration search paths, store stable IDs invisibly after selection, and deliver one independently testable resource slice at a time.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy/SQLModel, pytest, Next.js, React, TypeScript, TanStack Query, shadcn/Radix, Jest, Playwright

---

## 1. Product decision

### 1.1 Do not automatically save `Owner = me`

Selecting **Deal / Created** and **Run as me** must immediately show this locked rule:

> **Applies to: Deals you own or created**
>
> Enforced by Run as me

This is an execution-scope rule, not an optional filter. The backend currently authorizes a personal record event when the user is either `owner_id` **or** `created_by_user_id`. Automatically saving `Owner = me` would incorrectly exclude a deal that the user created and later assigned to somebody else. It would also leave a misleading personal filter behind when an admin switches the trigger to **Run as organisation**.

The optional-filter area may provide quick actions:

- **Owner is me** — adds an explicit owner-only condition.
- **Created by me** — adds an explicit creator-only condition.

Those actions narrow the locked personal scope; they never create it.

### 1.2 Email follows the same separation

For **Email / Received** with **Run as me**, show:

> **Applies to: All active email accounts connected by you**
>
> Choose a mailbox below only if this trigger should watch fewer accounts.

The default mailbox selection is **All my connected mailboxes**. A mailbox picker displays addresses such as `nikhil@company.com`, never integration IDs. Selecting a mailbox stores its stable integration ID invisibly.

For an eligible admin using **Run as organisation**, show:

> **Applies to: All active organisation email accounts**

The admin may then narrow the trigger to one or more organisation mailboxes. Members can use **Run as me** but cannot select or submit **Run as organisation**.

### 1.3 Authority rules

| Caller | Run as me | Run as organisation |
|---|---|---|
| Member | Allowed when the agent is personally eligible | Never allowed |
| Admin | Allowed when the agent is personally eligible | Allowed only when the agent is organisation-eligible |

Organisation execution uses the organisation service principal. It is not “run as the admin.” Audience controls determine who can see the run; they do not change which events match.

---

## 2. Current catalog inventory

The current producer-backed catalog contains 49 definitions across these resources:

- System: Manual
- Time: Schedule
- CRM: Deal, Contact, Account
- Communication: Email
- Conversation: Meeting
- Integration: Webhook
- AI: Deal risk, Deal blocker
- Agent: Run

Lead, Task, Note, email reply, and the remaining meeting lifecycle definitions are not currently producer-backed and must not be added by this plan.

### 2.1 Current business fields

| Resource | Keep as normal business filters | Needs a resolver or controlled options |
|---|---|---|
| Deal | Name, amount, expected close date | Deal, pipeline, stage, status, currency, owner, creator, account, primary contact |
| Contact | Name, email, phone | Contact, owner, account, lifecycle |
| Account | Name, domain, annual revenue | Account, owner, lifecycle, tier |
| Email | Sender, sender domain, recipients, attachments | Connected mailbox, related deal |
| Meeting | Provider, language | Owner/organiser, attendees, teams, related deal |
| Deal risk/blocker | Status, severity, confidence | Deal, buyer |
| Agent run | Causing event | Agent, trigger, created by, outcome |
| Schedule | Start, recurrence, interval | Timezone, optional CRM targets |
| Webhook | Payload schema | No business filter resolver required |

### 2.2 Fields to hide from the standard filter list

These values remain in the durable event envelope for validation, matching compatibility, diagnostics, and idempotency. Hiding them is a catalog-presentation change, not data deletion.

**Hide everywhere:**

- Actor type
- Actor ID, except when represented as the friendly Email mailbox field
- Source domain
- Source system
- Source operation
- Source mode
- Source event ID

**Hide impossible or duplicate update fields:**

- Deal ID before / after
- Contact ID before / after
- Account ID before / after

Record identity cannot change during an update. These fields add noise without a valid business scenario.

**Hide resource-specific technical fields:**

| Resource | Hide |
|---|---|
| Email | Email ID, Email content ID, duplicate Direction, raw Thread ID |
| Meeting | Meeting ID, duplicate Meeting reference ID |
| Deal risk/blocker | Subject ID, Signal ID, Signal type, Evidence ID, Model, Prompt version, Inference run ID |
| Agent run | Run ID |
| Manual | All generic actor/source metadata |
| Schedule | Generic actor/source metadata; scheduled time and timezone belong in Configuration, not Optional filters |
| Webhook | Actor/source metadata and dedupe key |

No hidden ID should be moved into an “Advanced” drawer merely because it exists. Advanced fields require a real support or power-user scenario. For this first pass, the plan keeps raw technical identifiers hidden entirely.

### 2.3 Display labels versus stored values

| Visible control | User sees | Request stores |
|---|---|---|
| Mailbox picker | `sales@acme.com` | Integration UUID |
| Deal picker | `Renewal — Acme` | Deal UUID |
| Contact picker | `Maya Chen — maya@acme.com` | Contact UUID |
| Account picker | `Acme — acme.com` | Account UUID |
| User picker | `Alex Morgan — alex@company.com` | User UUID |
| Pipeline/stage picker | `Enterprise / Negotiation` | Pipeline or stage UUID |
| Agent picker | `Deal Stage Health Review` | Agent UUID |
| Trigger picker | `CRM / Deal / Stage changed — Renewal health` | Trigger UUID |

The UI must resolve and retain the label after editing a saved trigger. A raw UUID must never flash while options are loading.

---

## 3. Catalog contract

### 3.1 Add scope presentation metadata

Each catalog definition returns backend-owned scope copy:

```json
{
  "scope_presentation": {
    "personal": "Deals you own or created",
    "organisation": "All organisation deals"
  }
}
```

Email returns “All active email accounts connected by you” and “All active organisation email accounts.” Meeting returns “Meetings you organise or own” and “All organisation meetings.” Agent run returns “Runs created by you” and “All organisation runs.” Manual, schedule, and webhook use family-specific execution copy because they do not begin with a matched record.

### 3.2 Add field visibility

Catalog fields use only:

```python
FilterVisibility = Literal["standard", "advanced", "hidden"]
```

- `standard`: shown when the user clicks **Add condition**.
- `advanced`: shown only inside an explicitly opened advanced group.
- `hidden`: supported by the matcher for compatibility but omitted from the catalog response.

This phase does not expose any `advanced` technical identifiers. The value exists so a later proven use case does not require another schema redesign.

### 3.3 Use option sources for business entities

Use the existing `option_source` contract instead of adding trigger-specific frontend key lists:

| Option source | Control |
|---|---|
| `pipelines` | Pipeline select |
| `pipeline_stages` | Pipeline/stage select |
| `crm_users` | CRM owner picker |
| `deals` | Async deal search |
| `contacts` | Async contact search |
| `accounts` | Async account search |
| `email_mailboxes` | Authority-aware mailbox multi-select |
| `organisation_users` | Active workspace member picker |
| `agents` | Accessible agent search |
| `automation_triggers` | Accessible trigger search |
| `risk_statuses` | Bounded select |
| `risk_severities` | Bounded select |
| `agent_run_statuses` | Bounded select |
| `trigger_event_keys` | Friendly event breadcrumb select |
| `contact_lifecycle_values` | Live distinct-value combobox |
| `account_lifecycle_values` | Live distinct-value combobox |
| `account_tier_values` | Live distinct-value combobox |
| `deal_currency_values` | Live distinct-value combobox |

Large entity collections are searched; they are not embedded wholesale in the catalog. Small bounded and distinct-value collections may remain in `option_sets.enums`.

---

## 4. Delivery strategy

Commit the current catalog-driven trigger picker as a checkpoint after its existing focused verification is fresh and the mixed backend index is separated. Do not include this plan’s implementation in that checkpoint.

Then deliver the following vertical slices independently:

1. Scope presentation and technical-field hiding.
2. Deal business filters.
3. Email mailbox and address filters.
4. Contact and Account filters.
5. Meeting, AI insight, and Agent run filters.
6. Full authority and browser matrix.

Each slice must be releasable and reversible without waiting for later slices.

---

### Task 1: Add scope and visibility to the backend catalog

**Files:**

- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/catalog.py`
- Modify: `src/api/schemas/automation_trigger_models.py`
- Test: `tests/service/automation/triggers/test_catalog.py`

- [ ] **Step 1: Add failing catalog contract tests**

Add tests asserting that Deal Created includes both scope labels, `actor.id` is absent, `changes.id.before` is absent, and `record.owner_id` remains visible.

```python
def test_deal_catalog_exposes_scope_without_technical_fields() -> None:
    definition = _entries(build_trigger_catalog([]))["crm.deal.created"]
    assert definition.scope_presentation.personal == "Deals you own or created"
    assert definition.scope_presentation.organisation == "All organisation deals"
    paths = {field.path for field in definition.fields}
    assert "record.owner_id" in paths
    assert "actor.id" not in paths
    assert "source.source_event_id" not in paths


def test_updated_catalog_hides_immutable_identity_changes() -> None:
    definition = _entries(build_trigger_catalog([]))["crm.deal.updated"]
    paths = {field.path for field in definition.fields}
    assert "record.id" in paths
    assert "changes.id.before" not in paths
    assert "changes.id.after" not in paths
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/automation/triggers/test_catalog.py -q
```

Expected: failure because scope presentation and field visibility are not yet serialized.

- [ ] **Step 3: Add the minimal typed catalog metadata**

Add the following fields and propagate them through `_path_field`, `_change_field`, and catalog serialization:

```python
FilterVisibility = Literal["standard", "advanced", "hidden"]


@dataclass(frozen=True)
class RecordField:
    # Existing fields remain unchanged.
    filter_visibility: FilterVisibility = "standard"


@dataclass(frozen=True)
class FieldDefinition:
    # Existing fields remain unchanged.
    filter_visibility: FilterVisibility = "standard"
```

Add the response models:

```python
class TriggerCatalogScopePresentation(BaseModel):
    personal: str | None = None
    organisation: str | None = None


class TriggerCatalogField(BaseModel):
    # Existing fields remain unchanged.
    visibility: Literal["standard", "advanced"] = "standard"


class TriggerCatalogDefinition(BaseModel):
    # Existing fields remain unchanged.
    scope_presentation: TriggerCatalogScopePresentation
```

Catalog serialization omits hidden fields but the registry keeps them available to deterministic matching:

```python
if item.filter_visibility == "hidden":
    continue
```

- [ ] **Step 4: Define scope copy once by subject/family**

In `catalog.py`, add one mapping function rather than repeating copy across 49 trigger definitions:

```python
_PERSONAL_SCOPE = {
    "deal": "Deals you own or created",
    "contact": "Contacts you own",
    "account": "Accounts you own",
    "email": "All active email accounts connected by you",
    "meeting": "Meetings you organise or own",
    "agent_run": "Runs created by you",
}

_ORGANISATION_SCOPE = {
    "deal": "All organisation deals",
    "contact": "All organisation contacts",
    "account": "All organisation accounts",
    "email": "All active organisation email accounts",
    "meeting": "All organisation meetings",
    "agent_run": "All organisation runs",
}
```

Manual, schedule, and webhook return family-specific copy. Return `None` for a principal mode the definition does not allow.

- [ ] **Step 5: Hide common metadata and immutable ID changes**

Mark `_COMMON_EVENT_FIELDS` hidden. Mark each base record ID as visible but non-change-filterable by separating update-change visibility from record visibility:

```python
@dataclass(frozen=True)
class RecordField:
    # Existing fields remain unchanged.
    include_change_filters: bool = True


def _id_field(label: str = "ID", *, option_source: str | None = None) -> RecordField:
    return RecordField(
        "id",
        label,
        FieldType.UUID,
        required=True,
        exact=True,
        option_source=option_source,
        include_change_filters=False,
    )
```

Generate before/after filters only when both `filterable` and `include_change_filters` are true.

- [ ] **Step 6: Re-run catalog and registry tests**

Run:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/automation/triggers/test_catalog.py tests/service/automation/triggers/test_registry.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit the backend contract slice**

```powershell
git -C D:\CopilotGTM\copilot-backend add src/service/automation/triggers/registry.py src/service/automation/triggers/catalog.py src/api/schemas/automation_trigger_models.py tests/service/automation/triggers/test_catalog.py tests/service/automation/triggers/test_registry.py
git -C D:\CopilotGTM\copilot-backend commit -m "feat: add business trigger filter metadata"
```

---

### Task 2: Render the locked execution scope and visible field groups

**Files:**

- Modify: `D:/CopilotGTM/copilot-app-frontend/src/modules/agents-v1/types/agents-v1.types.ts`
- Modify: `D:/CopilotGTM/copilot-app-frontend/src/modules/agents-v1/types/agents-v1.schemas.ts`
- Modify: `D:/CopilotGTM/copilot-app-frontend/src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Test: `D:/CopilotGTM/copilot-app-frontend/__tests__/unit/modules/agents-v1/agents-v1-trigger-contract.test.ts`
- Test: `D:/CopilotGTM/copilot-app-frontend/__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`

- [ ] **Step 1: Add failing frontend contract and rendering tests**

```tsx
it("shows personal scope as a locked rule without saving an owner filter", async () => {
  renderBuilder({ definition: dealCreatedDefinition, isAdmin: false });
  expect(screen.getByText("Deals you own or created")).toBeInTheDocument();
  expect(screen.getByText("Enforced by Run as me")).toBeInTheDocument();
  expect(currentFormValues().filter).toBeNull();
});

it("does not offer hidden technical fields", async () => {
  renderBuilder({ definition: dealCreatedDefinition, isAdmin: true });
  await userEvent.click(screen.getByRole("button", { name: "Add condition" }));
  expect(screen.queryByText("Actor ID")).not.toBeInTheDocument();
  expect(screen.queryByText("Source event ID")).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```powershell
yarn test:jest __tests__/unit/modules/agents-v1/agents-v1-trigger-contract.test.ts __tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx --runInBand
```

Expected: failure because the current schema and builder do not know scope presentation or visibility.

- [ ] **Step 3: Parse the backend-owned metadata**

Add matching TypeScript types and schema parsing:

```ts
type TriggerCatalogScopePresentation = {
  personal: string | null;
  organisation: string | null;
};

type TriggerCatalogField = {
  // Existing fields remain unchanged.
  visibility: "standard" | "advanced";
};
```

- [ ] **Step 4: Render scope separately from filters**

Under the authority cards, render a locked **Applies to** row using `definition.scope_presentation[values.principal_mode]`. Do not mutate `values.filter` when authority changes.

```tsx
<div className="rounded-lg border bg-surface-muted p-3">
  <p className="text-xs font-medium text-muted-foreground">Applies to</p>
  <p className="text-sm font-medium">{scopeLabel}</p>
  <p className="text-xs text-muted-foreground">
    Enforced by {values.principal_mode === "personal" ? "Run as me" : "Run as organisation"}
  </p>
</div>
```

- [ ] **Step 5: Render standard fields only in the default picker**

Filter the Add condition field list by `visibility === "standard"`. Render an **Advanced filters** disclosure only when the selected definition actually contains advanced fields.

- [ ] **Step 6: Verify tests and types**

```powershell
yarn test:jest __tests__/unit/modules/agents-v1/agents-v1-trigger-contract.test.ts __tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx --runInBand
yarn check-types
```

Expected: tests and TypeScript pass.

- [ ] **Step 7: Commit the frontend scope slice**

```powershell
git -C D:\CopilotGTM\copilot-app-frontend add src/modules/agents-v1/types/agents-v1.types.ts src/modules/agents-v1/types/agents-v1.schemas.ts src/modules/agents-v1/components/trigger-builder-sheet.tsx __tests__/unit/modules/agents-v1/agents-v1-trigger-contract.test.ts __tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx
git -C D:\CopilotGTM\copilot-app-frontend commit -m "feat: explain trigger execution scope"
```

---

### Task 3: Deliver business-friendly Deal filters

**Files:**

- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/options.py`
- Test: `tests/service/automation/triggers/test_catalog.py`
- Test: `tests/service/automation/triggers/test_matcher_validation.py`
- Modify: `D:/CopilotGTM/copilot-app-frontend/src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Test: `D:/CopilotGTM/copilot-app-frontend/__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`

- [ ] **Step 1: Add failing option-source tests**

Assert this mapping:

```python
expected = {
    "record.id": "deals",
    "record.pipeline_id": "pipelines",
    "record.stage_id": "pipeline_stages",
    "record.deal_status": "deal_statuses",
    "record.owner_id": "crm_users",
    "record.created_by_user_id": "crm_users",
    "record.account_id": "accounts",
    "record.primary_contact_id": "contacts",
}
```

Before/after fields inherit the same source. Add tenant-bound validation cases proving a foreign/deleted Deal, Account, or Contact ID is rejected.

- [ ] **Step 2: Add only the missing registry sources**

Set Deal ID to `deals`, Account to `accounts`, and Primary contact to `contacts`. Keep existing pipelines, stages, statuses, and CRM users unchanged.

- [ ] **Step 3: Extend option validation at the existing boundary**

In `AutomationTriggerOptionAccess.validate_option_selection`, validate IDs using organisation-scoped, non-deleted rows. Do not add a second validation service.

- [ ] **Step 4: Reuse existing frontend search hooks**

Extend the current record picker pattern to route:

```ts
const entityTypeBySource = {
  deals: "deal",
  contacts: "contact",
  accounts: "account",
} as const;
```

Use `useDealSearch`, `useContactSearch`, and `useAccountSearch`. Render labels with existing response fields and store only the chosen UUID.

- [ ] **Step 5: Add explicit quick filters without changing authority**

For CRM resources, show **Owner is me** and **Created by me** shortcuts only when those fields exist. Clicking one creates an ordinary condition; opening the trigger does not.

- [ ] **Step 6: Verify the Deal slice**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/automation/triggers/test_catalog.py tests/service/automation/triggers/test_matcher_validation.py -q
yarn test:jest __tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx --runInBand
yarn check-types
```

Expected: all selected checks pass, and no Deal relation requires manual UUID entry.

- [ ] **Step 7: Commit Deal backend and frontend changes separately**

- Backend commit: `feat: add deal trigger option sources`
- Frontend commit: `feat: add searchable deal trigger filters`

---

### Task 4: Deliver Email mailbox and address filters

**Files:**

- Modify: `src/service/automation/producers/email.py`
- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/options.py`
- Modify: `src/service/automation/triggers/catalog.py`
- Modify: `src/api/schemas/automation_trigger_models.py`
- Modify: `src/service/automation/triggers/service.py`
- Test: `tests/service/automation/producers/test_email.py`
- Test: `tests/service/automation/triggers/test_catalog.py`
- Test: `tests/service/automation/triggers/test_matcher_validation.py`
- Modify: `D:/CopilotGTM/copilot-app-frontend/src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Modify: `D:/CopilotGTM/copilot-app-frontend/src/modules/agents-v1/types/agents-v1.types.ts`
- Modify: `D:/CopilotGTM/copilot-app-frontend/src/modules/agents-v1/types/agents-v1.schemas.ts`
- Test: `D:/CopilotGTM/copilot-app-frontend/__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`

- [ ] **Step 1: Add failing producer and catalog tests**

Assert that an email event contains a business record field called `mailbox_integration_id` equal to the existing integration actor ID, and that the catalog exposes it as **Mailbox** with source `email_mailboxes`. Assert raw Email ID, Email content, Direction duplicates, Thread ID, and Actor ID are absent.

```python
assert envelope.record["mailbox_integration_id"] == str(integration.integration_id)
assert fields["record.mailbox_integration_id"].label == "Mailbox"
assert fields["record.mailbox_integration_id"].option_source == "email_mailboxes"
```

- [ ] **Step 2: Add the minimal business alias to the event snapshot**

Add one explicit record field:

```python
"mailbox_integration_id": str(integration.integration_id),
```

This does not add a database column. It gives the business record snapshot a stable mailbox field while `actor.id` remains technical provenance.

- [ ] **Step 3: Return tenant-safe mailbox options**

Add a catalog option model containing only:

```python
class TriggerCatalogMailboxOption(BaseModel):
    integration_id: UUID
    label: str
    created_by_user_id: UUID
```

`AutomationTriggerOptionAccess.list_email_mailboxes(user_id, is_admin)` returns active Gmail/Outlook email integrations. Members receive only integrations they connected; admins receive organisation mailboxes so organisation mode can be configured. The label is `integration_email`, falling back to the integration name only when the address is absent.

- [ ] **Step 4: Validate mailbox selections server-side**

For personal bindings, require the mailbox integration’s `created_by_user_id` to equal the execution principal. For organisation bindings, require an active integration inside the organisation. Validation must not trust the frontend’s filtered option list.

The current validator checks option selections before resolving authority. Move the `_validate_authority` call immediately before the option-selection loop and pass the resolved context through the existing validation boundary:

```python
await self.options.validate_option_selection(
    source,
    values,
    principal_id=principal_id,
    organisation_scope=(
        principal_type == AutomationPrincipalType.ORGANISATION_SERVICE
    ),
)
```

Extend the protocol and concrete method with the same two required keyword arguments. Every existing option source continues using tenant validation. Only `email_mailboxes` adds the principal-owner check when `organisation_scope` is false. Add a validator regression test proving a personal binding cannot save another user’s otherwise valid organisation mailbox.

- [ ] **Step 5: Render mailbox and address controls**

- Mailbox: multi-select of human-readable addresses; default empty means all mailboxes inside the locked authority scope.
- Sender: email input that permits any valid external address; CRM-contact suggestions are optional assistance, not an allowlist.
- Recipients: tokenized multi-email input with the same open-entry behavior.
- Sender domain: normalized text input.
- Has attachments: Yes/No select.
- Related deal: existing async Deal picker.

Do not show Message ID or ask a user to paste an integration ID.

- [ ] **Step 6: Add authority behavior tests**

Cover these cases:

```text
Member + Run as me + no mailbox filter -> all active mailboxes connected by member
Member + Run as me + one mailbox -> only selected owned mailbox
Member + Run as organisation -> unavailable and backend rejected
Admin + Run as organisation + no mailbox filter -> all active organisation mailboxes
Admin + Run as organisation + selected mailbox -> only selected organisation mailbox
```

- [ ] **Step 7: Verify and commit Email as its own slice**

Run focused producer, catalog, matcher, validator, frontend component, TypeScript, and browser tests. Commit backend and frontend separately with messages `feat: add mailbox-aware email triggers` and `feat: add business email trigger filters`.

---

### Task 5: Deliver Contact and Account resolvers

**Files:**

- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/options.py`
- Test: `tests/service/automation/triggers/test_catalog.py`
- Test: `tests/service/automation/triggers/test_matcher_validation.py`
- Modify: `D:/CopilotGTM/copilot-app-frontend/src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Test: `D:/CopilotGTM/copilot-app-frontend/__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`

- [ ] **Step 1: Map entity relations**

```text
Contact record ID -> contacts
Contact owner -> crm_users
Contact account -> accounts
Account record ID -> accounts
Account owner -> crm_users
```

- [ ] **Step 2: Populate provider-defined values safely**

Return distinct non-empty tenant values for Contact lifecycle, Account lifecycle, and Account tier. Render searchable comboboxes; store the exact canonical value returned by the backend.

- [ ] **Step 3: Verify display and persistence**

Add tests proving a user selects `Acme — acme.com`, the form stores the UUID, editing resolves the label again, and a foreign UUID is rejected by backend validation.

- [ ] **Step 4: Run focused checks and commit**

- Backend commit: `feat: add contact and account trigger options`
- Frontend commit: `feat: add contact and account trigger pickers`

---

### Task 6: Deliver Meeting, AI insight, and Agent run filters

**Files:**

- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/options.py`
- Modify: `src/service/automation/triggers/catalog.py`
- Test: `tests/service/automation/triggers/test_catalog.py`
- Test: `tests/service/automation/triggers/test_matcher_validation.py`
- Modify: `D:/CopilotGTM/copilot-app-frontend/src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Test: `D:/CopilotGTM/copilot-app-frontend/__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`

- [ ] **Step 1: Meeting**

Keep Provider and Language only when populated from controlled/distinct options. Map Owner and Attendees to active organisation users, Teams to CRM teams, and Related deal to Deal search. Hide Meeting IDs and duplicate references.

- [ ] **Step 2: AI risk/blocker**

Keep Status, Severity, Confidence, Deal, and Buyer. Populate Status and Severity from the domain enums; map Deal and Buyer to searches. Hide signal identity, model provenance, evidence, prompt version, inference run, and redundant signal type.

- [ ] **Step 3: Agent run**

Keep Agent, Trigger, Outcome, Causing event, and Created by. Resolve Agent and Trigger names, show friendly event breadcrumbs, populate run outcomes from `AgentRunStatus`, and map Created by to active organisation users. Hide Run ID.

- [ ] **Step 4: Test every resolver boundary**

For each option source, prove:

- active tenant value accepted;
- foreign tenant value rejected;
- deleted/inactive value rejected where applicable;
- saved UUID renders its label when editing;
- loading never exposes the raw UUID as visible text.

- [ ] **Step 5: Run focused checks and commit resource slices separately**

Use one backend and one frontend commit per resource group so Meeting, AI, or Agent run can be reverted independently.

---

### Task 7: Verify authority, accessibility, and the complete catalog matrix

**Files:**

- Modify: `D:/CopilotGTM/copilot-app-frontend/__tests__/e2e/agents-v1.spec.ts`
- Modify: `D:/CopilotGTM/copilot-app-frontend/__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`
- Modify: `tests/service/automation/triggers/test_catalog.py`
- Modify: `tests/service/automation/triggers/test_validator.py`

- [ ] **Step 1: Add a backend catalog matrix assertion**

Iterate every producer-backed definition and fail if:

- a standard UUID/relation field lacks an approved option source;
- a hidden field appears in the serialized catalog;
- an allowed principal mode lacks scope copy;
- an organisation-only trigger advertises personal scope.

- [ ] **Step 2: Add authority matrix tests**

Assert members cannot submit organisation mode, admins can only submit it when agent eligibility permits, and switching authority updates the locked scope label without inserting or retaining an implicit owner/mailbox filter.

- [ ] **Step 3: Add browser walking skeletons**

Exercise:

1. Deal Created / Run as me / Owner is me quick filter.
2. Email Received / Run as me / one connected mailbox / sender filter.
3. Deal Stage changed / Run as organisation as an eligible admin.
4. A member who never receives a selectable organisation authority.

Verify visible labels and inspect the request payload to prove UUIDs are stored but never shown as input text.

- [ ] **Step 4: Run final verification**

Backend:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/automation/producers/test_email.py tests/service/automation/triggers -q
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m ruff check src/service/automation/producers/email.py src/service/automation/triggers tests/service/automation
git -C D:\CopilotGTM\copilot-backend diff --check
```

Frontend:

```powershell
yarn test:jest __tests__/unit/modules/agents-v1 --runInBand
yarn check-types
yarn lint
yarn build
git -C D:\CopilotGTM\copilot-app-frontend diff --check
```

Browser:

```powershell
yarn playwright test __tests__/e2e/agents-v1.spec.ts
```

Expected: all commands pass. Record focused versus full-suite evidence separately.

---

## 5. Acceptance criteria

1. Deal Created with Run as me shows “Deals you own or created” without saving an automatic filter.
2. Owner is me remains available as an explicit one-click narrowing condition.
3. Email Received with Run as me defaults to all active email integrations connected by that user.
4. A user with multiple mailboxes can select them by email address.
5. An admin can select organisation mailboxes only when organisation execution is eligible.
6. A member cannot select or submit Run as organisation.
7. No standard filter asks a user to type a UUID or internal record ID.
8. Deal, Contact, Account, Agent, Trigger, User, and Mailbox values display business labels and store stable IDs invisibly.
9. Raw actor/source metadata is absent from the business filter picker.
10. Immutable before/after identity fields are absent.
11. Duplicate Email and Meeting identity/direction fields are absent.
12. Switching personal/organisation authority updates scope copy and does not mutate optional filters.
13. Every exposed option source is validated inside the current tenant and authority boundary.
14. Editing a saved trigger resolves labels without flashing raw IDs.
15. Catalog, component, contract, and browser matrix tests prevent a new unresolved ID field from appearing later.

---

## 6. Explicit non-goals

- No new automation database table.
- No replacement of stable IDs with mutable names in persisted trigger filters.
- No frontend-maintained list of trigger keys.
- No producer work for Lead, Task, Note, email reply, or unsupported meeting lifecycle events.
- No explicit-user organisation audience picker until tenant-safe active-member discovery is approved for that separate feature.
- No release claim based only on unit tests; browser verification remains required for the completed resource slice.

---

## 7. Recommended checkpoint decision

Commit the current trigger foundation first after fresh verification and careful staging in each repository. Treat it as the catalog-driven trigger foundation, not the final business-filter experience. Implement this document afterward beginning with Task 1 and stop after any completed resource slice if priorities change.
