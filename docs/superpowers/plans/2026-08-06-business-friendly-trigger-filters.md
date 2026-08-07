# Business-Friendly Trigger Scope and Filters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task by task. Use `superpowers:test-driven-development` for each behavior change and `superpowers:verification-before-completion` before each checkpoint.

**Status:** Final development contract after repository and trigger-by-trigger loophole audit

**Goal:** Make all currently executable Agent Studio triggers understandable and safe for business users: show the automatic execution boundary, replace selectable identifiers with searchable labels, expose only conditions the event can truthfully provide, and keep organisation execution admin-only.

**Architecture:** The backend catalog remains authoritative. The UI presents four separate concepts in order: event, execution authority, optional source/record selection, and optional business conditions. Authority sets the maximum security boundary. Source selection and conditions may only narrow it. Stable IDs remain internal request values and never become user-entered text. The current development catalog is replaced atomically by one business-facing contract without changing the durable event envelope.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy/SQLModel, pytest, Next.js, React, TypeScript, TanStack Query, shadcn/Radix, Jest, Playwright

---

## 1. Executive decision

### 1.1 The user's instinct is right, but the automatic Deal rule must be truthful

When a user chooses **Deal / Created** and **Run as me**, the UI must add this locked, visible scope automatically:

> **Applies to: Deals you own or created**<br>
> Enforced by Run as me

It must **not** save an editable `Owner equals me` condition. The backend personal-scope rule accepts a deal when the principal is either the event-time `owner_id` or `created_by_user_id`. Saving only `Owner equals me` would silently exclude a deal created by the user and later assigned to somebody else.

The optional-condition area may offer two friendly shortcuts:

- **Only deals I own** adds an explicit owner condition.
- **Only deals I created** adds an explicit creator condition.

Those shortcuts narrow the locked scope. They do not define authority.

### 1.2 Email uses the same separation

For **Email / Email received** and **Run as me**, show:

> **Applies to: New messages in active mailboxes connected by you**<br>
> Mailboxes: All my connected mailboxes

The user may change **All my connected mailboxes** to **Specific mailboxes** and select labels such as `sales@acme.com · Google`. The stored integration UUID is never displayed or typed.

For **Run as organisation**, show:

> **Applies to: New messages in all active organisation mailboxes**

Only an organisation admin may select or submit this mode. Selecting organisation execution does not mean “run as the admin”; it uses the organisation service principal.

### 1.3 Four concepts must never be collapsed into one control

| Concept | Business question | Effect | Can broaden authority? |
| --- | --- | --- | --- |
| Event | What happened? | Chooses the durable fact, such as Deal created or Email received | No |
| Execution authority | Whose permissions and records may this run use? | Selects personal or organisation principal and the maximum scope | Yes, but organisation mode is admin-only and backend-authorized |
| Source/record selection | Which mailboxes, records, deals, or agents should be watched? | Adds a server-compiled fixed narrowing rule | No |
| Optional conditions | Under what business conditions? | Adds deterministic matcher predicates | No |

Audience is separate again: it controls who can see an organisation run, not which event activates it.

### 1.4 Checkpoint decision

The current catalog-driven picker is a valuable baseline and should be committed before this tranche **only after its existing focused backend and frontend checks are fresh and the staged diff contains only that baseline**. Do not mix partial implementation of this document into that checkpoint. The document itself may be committed as a documentation-only checkpoint.

No commit, push, deployment, migration, or application-code change is part of writing this plan.

---

## 2. Audited current state

### 2.1 Exact advertised inventory

The current catalog advertises **48**, not 49, definitions as producer-backed:

| Source | Count |
| --- | ---: |
| Manual | 1 |
| Schedule | 1 |
| Deal | 16 |
| Contact | 9 |
| Account | 8 |
| Email | 2 |
| Meeting | 2 |
| Webhook | 1 |
| AI deal risk/blocker | 6 |
| Agent run | 2 |
| **Total** | **48** |

Do not add Lead, Task, Note, Email replied, Meeting scheduled/rescheduled/cancelled/ended, conversation messages, buyer signals, Agent run cancelled, or approval triggers in this tranche. Their producers are not currently available.

### 2.2 Event triggers are forward-looking

CRM and email initial-sync/backfill modes do not activate user bindings. The UI must state:

> Starts with new activity after this trigger is active. Existing imported or backfilled records do not run the agent.

This is especially important for a newly connected mailbox: importing old messages is not the same as receiving new mail.

### 2.3 Current editor problems this plan closes

- The frontend currently offers raw ID and technical event paths as ordinary fields.
- Empty or unknown option sources fall back to a text box, so a business user is asked to type UUIDs.
- `in`, `not_in`, and `between` are represented as comma-separated text instead of typed controls.
- Watch paths are record keys such as `stage_id`, while fields are paths such as `record.stage_id`; labels can fall back to raw keys.
- Personal authority copy incorrectly says “own or created” for Contact and Account, although those events contain only owner scope.
- Hidden fields omitted from the catalog would make the current definition-switch logic silently clear saved conditions.
- Search results do not hydrate selected values that are outside the current page, so edit screens can lose labels.
- Fixed watch/filter decisions are stored in the effective matcher. The detail API must return the user-authored decision, not send fixed rules back as editable values and duplicate them on replace.

### 2.4 Runtime-catalog gate discovered by this audit

`crm.account.tier_changed` is advertised, but the current Account write path neither accepts `tier` as an update field nor includes it in `AccountMutationPolicy.semantic_for_update()`.

**Decision for this tranche:** set this definition's `producer_available=False` and remove it from the returned catalog. Do not invent a tier write path merely to preserve the picker count. A later independently scoped producer change may re-enable it after an actual Account tier mutation reaches `crm.account.updated` with `changes.tier` in a regression test.

The current advertised count is 48; the truthful release count for this plan is **47**.

### 2.5 External CRM source-parity migration boundary

The native Deal, Contact, and Account write services record CRM mutation events. The current HubSpot and Salesforce record writers update the CRM rows directly and do not call `CRMMutationEventService`; HubSpot delete webhook handlers also soft-delete rows directly. Therefore the current generic CRM labels do **not** yet prove that incremental provider changes activate these triggers.

A business user could read **Deal created** as “a deal was created in any connected CRM or in CopilotGTM.” This tranche must not imply that yet.

**Decision:** HubSpot and Salesforce trigger emission belongs to a separate provider-migration/adapter plan. This implementation does not modify provider writers, webhook handlers, or CDC workers. Until that migration is delivered, the frontend labels CRM triggers as **Changes recorded in CopilotGTM** and does not claim connected-CRM source parity.

The later provider migration must deliver these invariants together:

- native CopilotGTM creates/updates/deletes emit interactive events;
- HubSpot and Salesforce incremental creates/updates/deletes emit incremental-sync events;
- initial sync, backfill, and deletion-reconciliation scans do not persist guaranteed-no-op automation dispatches;
- every event is recorded in the same transaction as its CRM row mutation;
- provider retries reuse stable idempotency inputs;
- only registry-approved record/change fields enter the envelope.

Implement that future source parity through one shared snapshot-diff recorder, not six provider-specific event builders. The recorder loads one tenant registry snapshot per writer transaction, compares the policy-owned automation records before and after the write, and delegates persistence to `CRMMutationEventService`. It must suppress initial/backfill before ledger/outbox creation.

```python
async def record_transition(
    self,
    *,
    entity_type: Literal["deal", "contact", "account"],
    entity_id: UUID,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    integration_id: UUID,
    provider: Literal["hubspot", "salesforce"],
    source_mode: AutomationSourceMode,
    idempotency_scope: str,
) -> CRMMutationRecordResult:
    if source_mode in {AutomationSourceMode.INITIAL_SYNC, AutomationSourceMode.BACKFILL}:
        return CRMMutationRecordResult(event_id=None, changed=False)
    return await self._record_registry_bounded_difference(
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        integration_id=integration_id,
        provider=provider,
        source_mode=source_mode,
        idempotency_scope=idempotency_scope,
    )
```

This plan intentionally takes that fallback: CRM picker descriptions must explicitly say **Changes recorded in CopilotGTM**. Broad connected-CRM descriptions may be introduced only by the separately verified provider migration.

---

## 3. Definitive user experience

The selected-trigger form uses this order:

1. **When** — source and event breadcrumb.
2. **Built-in rule** — a business sentence owned by the backend.
3. **Execution authority** — Run as me or Run as organisation.
4. **Applies to** — locked, resource-specific scope sentence.
5. **Which sources/records?** — optional friendly selector where the event supports it.
6. **Only if** — optional business conditions.
7. **Task goal** — what the agent should do.
8. **Active** — whether future qualifying facts may activate it.

### 3.1 Deal / Created example

```text
When
CRM / Deal / Created

Timing
Starts with deals created after this trigger is active.

Execution authority
[ Run as me ]  [ Run as organisation — admins only ]

Applies to
🔒 Deals you own or created
   Enforced by Run as me

Only if (optional)
[ Amount ] [ Is at least ] [ 50,000 ]
[ Stage  ] [ Is          ] [ Enterprise · Discovery ]

Task goal
[ Review the deal and recommend the next action. ]
```

There is no specific-deal selector for **Created** because the future deal ID does not exist when the binding is saved.

### 3.2 Email / Received example

```text
When
Communication / Email / Email received

Timing
Starts with new inbound mail after this trigger is active. Imported history is excluded.

Execution authority
[ Run as me ]  [ Run as organisation — admins only ]

Applies to
🔒 New messages in active mailboxes connected by you

Mailboxes
(•) All my connected mailboxes
( ) Specific mailboxes  [ sales@acme.com · Google ]

Only if (optional)
[ Sender domain ] [ Is ]          [ customer.com ]
[ Recipients     ] [ Includes any ] [ renewals@acme.com ]
[ Has attachments ] [ Is ]        [ Yes ]
```

Email ID, content ID, thread ID, mailbox owner ID, actor ID, source event ID, and source mode are not offered.

### 3.3 Empty-option behavior

An entity field never falls back to raw text. If the option source has no selectable values:

- disable the field or selector;
- show a backend-provided reason such as **Connect an email account first** or **No active lifecycle values are available**;
- preserve an existing unavailable selection as a labelled warning until the user explicitly removes or replaces it;
- never display its stored UUID.

---

## 4. Authority and scope contract

### 4.1 Who may select each mode

| Caller | Run as me | Run as organisation |
| --- | --- | --- |
| Member | Allowed when agent eligibility permits personal execution | Hidden or disabled; backend always rejects submission |
| Admin | Allowed when agent eligibility permits personal execution | Allowed only when the definition and agent eligibility both permit it |

The frontend uses server-provided `automation_eligibility`; it does not infer safety from tool counts or hardcoded tool names. The backend remains fail-closed if a stale or modified client submits an invalid combination.

### 4.2 Exact meaning of Run as me

| Resource/family | Personal event-time scope | Important consequence |
| --- | --- | --- |
| Deal | `owner_id = me` **or** `created_by_user_id = me` | A deal created by me may run after reassignment; owner-only is an optional narrowing condition |
| Contact | `owner_id = me` | An unassigned contact does not run; “created by me” is not supported by the event snapshot |
| Account | `owner_id = me` | An unassigned account does not run; “created by me” is not supported |
| Email | Mailbox integration `created_by_user_id = me` | All active connected mailboxes are included unless narrowed |
| Meeting | Resolved internal organiser; if none, one active connected-calendar owner | Being an attendee alone does not activate a personal meeting trigger |
| Agent run | `created_by_user_id = me` | The monitored run must have been created under my user identity |
| Manual | The invoking user; selected Deal uses owner-or-creator, Contact/Account use owner | Saving a manual binding does not invoke it |
| Schedule | The saved personal principal; selected Deal uses owner-or-creator, Contact/Account use owner | A global schedule has no record scope; selected records are validated at save and due time |
| Webhook | The saved user principal | Anyone holding the signed webhook credential can cause a run under that user authority |
| AI risk/blocker | Not available | These definitions are organisation-only |

For connected-CRM events, an unresolved provider owner/creator cannot establish personal authority and is skipped fail-closed. Organisation mode still covers the tenant event. The UI explains this when a workspace user has no mapped CRM identity.

### 4.3 Ownership-change caveat

CRM update events use the post-update record for personal scope:

- a Contact or Account **Owner changed** trigger runs for the new owner, not the old owner;
- a Deal **Owner changed** trigger runs for the new owner and also for the deal creator;
- an old deal owner who is not the creator no longer passes personal scope after reassignment.

Show this note beside Owner changed when Run as me is selected. Users who need every ownership transfer must use admin-only Run as organisation.

### 4.4 Organisation execution

Organisation mode means all matching organisation facts, regardless of the admin's personal record ownership. It uses the organisation service principal and remains subject to agent eligibility restrictions. Organisation admins can manage shared organisation bindings.

Audience options:

- **No user history**: the run executes but is not placed in a user's personal run history.
- **Selected workspace members**: available after the active-member picker in this plan is implemented.
- **Subject participants**: shown only when the catalog advertises it; uses immutable event-time internal participant IDs.

Audience never changes matching or execution authority.

---

## 5. Single business-facing catalog contract

### 5.1 Clean development replacement

Agent Studio and its trigger rows are still development-only and disposable. Replace the current
catalog and write contract directly:

```http
GET /users/v1/automation-trigger-catalog
```

Rules:

- the endpoint returns one business-facing catalog shape;
- do not add `schema_version` query negotiation;
- do not add `catalog_schema_version` to create or replace requests;
- do not retain a legacy serializer, validation branch, telemetry meter, or compatibility adapter;
- recreate disposable development trigger rows if the contract change invalidates them;
- update backend and frontend together before this functionality is promoted beyond development.

The existing persisted `config_schema_version` projection is not a second user-visible trigger
format and is not changed by this UI tranche. New `selection` configuration is part of the one
current trigger config contract.

### 5.2 Backend-owned presentation types

Add these exact concepts to the single catalog schema:

```python
FilterVisibility = Literal["standard", "hidden"]
FilterFieldGroup = Literal["current", "previous", "new", "context"]
FilterInputType = Literal[
    "text",
    "email",
    "number",
    "date",
    "datetime",
    "boolean",
    "single_select",
    "multi_select",
    "entity_single_select",
    "entity_multi_select",
    "email_tokens",
    "between",
]


class TriggerCatalogScopePresentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    personal: str | None = None
    organisation: str | None = None


class TriggerCatalogRulePresentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    details: list[str] = Field(default_factory=list)


class TriggerCatalogWatchField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    label: str


class TriggerCatalogSourceSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    all_label_personal: str
    all_label_organisation: str
    selected_label: str
    option_source: str
    filter_path: str
    max_selected: int = Field(ge=1, le=100)


class TriggerCatalogField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    label: str
    description: str = ""
    value_type: str
    operators: list[str] = Field(min_length=1)
    input_type: FilterInputType
    option_source: str | None = None
    visibility: FilterVisibility
    group: FilterFieldGroup
    principal_modes: list[Literal["personal", "organisation"]]
    event_keys: list[str] = Field(min_length=1)
    required: bool = False
    exact: bool = False
```

`TriggerCatalogDefinition` returns:

```python
scope_presentation: TriggerCatalogScopePresentation
rule_presentation: TriggerCatalogRulePresentation | None
watch_fields: list[TriggerCatalogWatchField]
timing_note: str
source_selector: TriggerCatalogSourceSelector | None
filter_empty_reason: str | None
```

### 5.3 Hidden does not mean editable

`visibility="hidden"` fields remain in the single catalog so the frontend can recognize that a
path is backend-owned. They are excluded from **Add condition** and from watch selection.

Create and replace requests may use only standard fields advertised for the selected principal
mode. Hidden or unrecognized paths are rejected. Because development trigger rows are disposable,
there is no preserve/edit exception for an older hidden leaf and no masked compatibility UI.

### 5.4 Structured watch labels

Replace presentation use of `watch_paths: string[]` with `watch_fields: [{path, label}]`. Persisted watch requests continue to use raw record keys because the matcher contract does not change.

Immutable IDs and creator provenance are not watchable. Add a `watchable` flag to `RecordField`; `_id_field()` and `created_by_user_id` fields set it to `False`. Event validation still accepts their values in durable envelopes and backend-owned fixed matchers.

### 5.5 User decision versus compiled matcher

The API detail must expose only user-authored `watch` and `filter` decisions. Fixed rules remain in catalog presentation and effective storage.

Use the deterministic shape already produced by `combine_filter_trees` to recover the user filter without a table change:

```python
def extract_user_filter(
    fixed: Mapping[str, Any] | None,
    effective: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if fixed is None:
        return deepcopy(dict(effective)) if effective is not None else None
    if effective == fixed:
        return None
    if (
        isinstance(effective, Mapping)
        and set(effective) == {"all"}
        and isinstance(effective["all"], list)
        and len(effective["all"]) == 2
        and effective["all"][0] == fixed
        and isinstance(effective["all"][1], Mapping)
    ):
        return deepcopy(dict(effective["all"][1]))
    raise ValueError("stored trigger filter cannot be separated from its fixed rule")
```

For watch decisions:

- if `allow_user_watch=True`, return the stored watch as the user decision;
- otherwise return `watch=null`, because the stored watch is the fixed preset;
- if a stored matcher cannot be separated, mark the binding `needs_attention` instead of guessing.

This prevents semantic trigger edits from resubmitting fixed watches as illegal user watches or duplicating fixed filters.

---

## 6. Source selection and label-resolution contract

### 6.1 Persisted shape

All optional event-source selectors use one config shape:

```json
{
  "selection": {
    "mode": "all",
    "ids": []
  }
}
```

or:

```json
{
  "selection": {
    "mode": "selected",
    "ids": ["stable-id-1", "stable-id-2"]
  }
}
```

Rules:

- `all` requires an empty ID list;
- `selected` requires 1 to 100 unique IDs;
- every selectable definition supplies the explicit UI default `{mode: "all", ids: []}`;
- an omitted `selection` is the API shorthand for **All** and the server normalizes both forms to
  the same explicit stored config/hash;
- semantic config such as `stage_id` remains alongside `selection`;
- the server compiles selected IDs into a fixed `in` condition on the catalog's `filter_path`;
- the frontend never manufactures that filter node.

Selector mapping:

| Trigger family | Selector | Compiled hidden path |
| --- | --- | --- |
| CRM Updated, Deleted, and semantic updates | Specific records | `record.id` |
| Email received/sent | Connected mailboxes | `actor.id` |
| AI risk/blocker | Deals | `record.deal_id` |
| Agent run completed/failed | Agents | `record.agent_id` |

CRM Created and Created or updated do not offer a record selector. Meeting does not offer one until a tenant-scoped meeting search exists.

### 6.2 Reuse the email actor; do not change the event envelope

Email events already set `actor.type="integration"` and `actor.id=<integration_id>`. Mailbox selection compiles selected mailbox IDs into an `in` condition on `actor.id`. Do not add a duplicate `record.mailbox_integration_id`, event schema version, database column, or producer field.

This keeps queued and replayed envelopes compatible.

### 6.3 Authority-aware validation order

Current option validation occurs before authority resolution. Change the validator order:

1. validate JSON and load the definition;
2. compile config/watch/filter;
3. resolve and validate authority;
4. validate `selection.ids` against the resolved principal and scope;
5. validate ordinary filter options against tenant-owned option sources;
6. persist the effective matcher.

Source-selection rules:

| Option source | Personal validation | Organisation validation |
| --- | --- | --- |
| `deals` | active deal owned by or created by principal | active organisation deal; caller must be admin |
| `contacts` | active contact currently owned by principal | active organisation contact; caller must be admin |
| `accounts` | active account currently owned by principal | active organisation account; caller must be admin |
| `email_mailboxes` | active email integration created by principal | active organisation email integration; caller must be admin |
| `ai_deals` | Not selectable because AI triggers are organisation-only | active organisation deal; caller must be admin |
| `agents` | active agent accessible to principal | active organisation agent visible under admin policy |

Filter relation options such as **Related account** remain tenant-scoped rather than execution-scope-scoped; a personally scoped deal may legitimately reference an account the user does not own.

### 6.4 Search and batch resolution APIs

Do not teach the frontend authorization rules for each option source. Add catalog-context endpoints:

```http
POST /users/v1/automation-trigger-options/search
Content-Type: application/json
```

```json
{
  "trigger_key": "email.message.received",
  "control": "source_selector",
  "field_path": null,
  "principal_mode": "personal",
  "query": "sales",
  "limit": 25,
  "cursor": null
}
```

For a filter value, send `control="filter"` and `field_path="record.deal_id"`. Manual/Schedule record chips use `control="direct_subject"` with `field_path` set to `deal`, `contact`, or `account`; explicit organisation audiences use `control="audience"` with a null path. The backend derives the option source and policy from the registry; it never trusts a client-supplied source name. Search and resolve request bodies are excluded from application logs so names, email addresses, and stable IDs do not leak through request logging.

Batch edit-time resolution:

```http
POST /users/v1/automation-trigger-options/resolve
Content-Type: application/json
```

```json
{
  "requests": [
    {
      "trigger_key": "email.message.received",
      "trigger_id": "saved-trigger-id",
      "control": "source_selector",
      "field_path": null,
      "principal_mode": "personal",
      "values": ["stable-mailbox-id"]
    },
    {
      "trigger_key": "crm.deal.updated",
      "trigger_id": "saved-trigger-id",
      "control": "filter",
      "field_path": "record.account_id",
      "principal_mode": "personal",
      "values": ["stable-account-id"]
    }
  ]
}
```

Each returned item has this shape:

```json
{
  "value": "stable-mailbox-id",
  "label": "sales@acme.com · Google",
  "status": "active",
  "selectable": true
}
```

Allowed statuses are `active`, `inactive`, `deleted`, and `unavailable`.

Limits:

- search limit defaults to 25 and cannot exceed 50;
- resolve accepts at most 20 request groups, 100 values per group, and 200 values total;
- both endpoints use the existing authenticated read rate limit;
- foreign-tenant values are returned only as `unavailable` without a label that leaks tenant data;
- inactive/deleted rows may be labelled only when the row still belongs to the current tenant, `trigger_id` resolves to a binding the caller may access, and the requested value is present in that binding's saved selection/filter;
- create-form resolution without `trigger_id` may resolve active values only;
- search returns only active/selectable values;
- resolution batches by option source and never performs one query per chip.

### 6.5 Rename, delete, and edit behavior

- Stable ID remains stored when a label changes; the UI shows the current label.
- A selected inactive/deleted value appears as **Needs attention**, remains internally preserved, and is not selectable again.
- Saving is blocked until an unavailable source selection is removed or replaced.
- Hidden or unrecognized filter paths block save and are never rendered as editable controls.
- While loading, render a skeleton chip, not the raw value.
- Saved-trigger cards use the same batch resolver and never summarize with UUIDs.

### 6.6 Definitive option-source map

| Catalog option source | User-facing values | Selection policy |
| --- | --- | --- |
| `pipelines` | Active CRM pipeline labels | Inline catalog snapshot; tenant-scoped |
| `pipeline_stages` | `Pipeline · Stage` labels | Inline catalog snapshot; active stages only for new choices |
| `deal_statuses` | Open, Won, Lost, Closed | Bounded backend enum |
| `deal_currency_values` | Distinct active currency codes | Tenant-scoped, capped and sorted |
| `crm_users` | CRM user name and email | Tenant CRM-user search; selected inactive values resolve only from an accessible saved binding |
| `deals` | Deal name plus account when available | Tenant relation filter search; execution-scope policy only when used as the source selector |
| `contacts` | Contact name plus email | Tenant relation filter search; execution-scope policy only when used as the source selector |
| `accounts` | Account name plus domain | Tenant relation filter search; execution-scope policy only when used as the source selector |
| `contact_lifecycle_values` | Distinct lifecycle labels | Tenant-scoped, capped and sorted |
| `account_lifecycle_values` | Distinct lifecycle labels | Tenant-scoped, capped and sorted |
| `account_tier_values` | Distinct tier labels | Filters only; Tier changed trigger remains unavailable |
| `email_mailboxes` | Mailbox address plus provider | Authority-scoped active integrations |
| `organisation_users` | Active workspace member name and email | Tenant-scoped; admin required only when the control belongs to organisation authority/audience |
| `meeting_providers` | Provider labels | Bounded values produced by supported meeting integrations |
| `meeting_languages` | Transcript language labels/codes | Distinct tenant values; no free internal ID |
| `risk_statuses` | Open, In progress, Resolved, Dismissed, Won't fix | Bounded backend enum, narrowed per AI event |
| `risk_severities` | Low, Medium, High, Critical | Bounded backend enum |
| `agents` | Accessible active agent names | Personal access policy or admin organisation policy according to control context |
| `automation_triggers` | Friendly source/event breadcrumb plus saved trigger name | Only bindings the caller may access under trigger-management policy |
| `trigger_event_keys` | Friendly source/event breadcrumbs | Values derived from the returned catalog, never raw keys in the menu |
| `agent_run_failure_statuses` | Execution failed, Delivery failed, Output validation failed | Bounded backend enum |
| `custom_field:<field_id>` | Active custom-option labels | Tenant custom-field schema; deleted options use saved-binding resolution rules |

Distinct-value sources return at most 100 normalized non-empty values. If cardinality exceeds the cap, the endpoint uses search/pagination rather than embedding values in the catalog.

### 6.7 Saved-list presentation without N+1 detail requests

`AutomationTriggerSummary` adds a backend-built presentation object:

```python
class TriggerConditionPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_label: str
    group: FilterFieldGroup
    operator: str
    value_labels: list[str] = Field(default_factory=list)
    status: Literal["active", "needs_attention"] = "active"


class AutomationTriggerPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    breadcrumb: str
    authority_label: str
    scope_summary: str
    built_in_rule: str | None = None
    source_summary: str | None = None
    conditions: list[TriggerConditionPresentation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
```

The list service compiles all rows against one registry snapshot and batch-resolves option values across the page. It never fetches one trigger detail or one option row per card. Scalar values such as amount and date may be formatted directly; entity values use resolved labels. Unavailable values produce **Needs attention** without exposing the stored ID. The frontend still owns friendly operator wording and layout.

---

## 7. Typed condition editor

### 7.1 Editor by operator and field type

| Operator | Required editor |
| --- | --- |
| `eq`, `neq`, scalar comparison | Scalar editor from `input_type` |
| `in`, `not_in` | Multi-select or token editor; never comma-separated UUID text |
| `between` | Two typed inputs labelled From and To; block save when From > To |
| `contains` on list | One typed token/entity selection |
| `contains_any`, `contains_all` | Multi-token or entity multi-select |
| `is_empty`, `not_empty` | No value control |

Rules:

- email tokens are trimmed, case-folded, syntax-validated, and sent as arrays;
- numeric values remain canonical decimal strings in requests;
- dates use date input; datetimes retain an explicit timezone/offset;
- booleans show **Yes/No**, not `true/false`;
- unknown `input_type`, option source, or operator disables the row with **Unsupported catalog control**;
- no unsupported control falls back to text;
- fields are grouped by backend metadata as Current value, Previous value, New value, and Context;
- field paths remain opaque to React.

### 7.2 Built-in-rule contradictions

Fixed filters and user conditions are ANDed. The backend presentation must hide a field side that is fully fixed by a semantic preset:

- Entered stage hides Current stage and New stage; Previous stage remains available.
- Exited stage hides Previous stage; New stage remains available.
- Won/Lost/Reopened hide Current status and New status; Previous status remains available.
- Amount crossed threshold hides Previous amount, New amount, and Current amount; users needing a narrower amount transition should use Amount changed.

The backend rejects a user filter on a hidden fixed path even if a modified frontend submits it. This prevents obviously dead bindings without implementing a general symbolic satisfiability solver.

Authority-determined fields follow the same rule:

- Contact/Account personal mode hides Current owner and New owner because post-event owner must already be the principal; Previous owner remains available on update events.
- Meeting personal mode hides Organiser because personal authority already fixes it to the resolved owner.
- Agent-run personal mode hides Created by because it must already be the principal.
- Email mailbox owner remains hidden in both modes because authority and mailbox selection express it.

Those fields remain available in organisation mode where they genuinely narrow all-organisation scope.

### 7.3 Combined Created or updated semantics

For Deal, Contact, and Account **Created or updated**:

- creates always pass the watch gate because create events have no update watch evaluation;
- selected watch fields gate updates only;
- show the sentence **Creates always run; updates run only when one of the selected fields changes**;
- expose Current value fields only;
- hide Previous and New groups because those paths do not exist on create events;
- do not offer a specific-record selector because it would exclude all future creates.

---

## 8. Trigger-by-trigger behavior

### 8.1 Deal — 16 definitions

Personal scope for every Deal definition is **Deals you own or created**. Organisation scope is **All organisation deals**.

Standard current-value conditions are Deal name, pipeline, stage, status, amount, currency, owner, created by, expected close date, account, primary contact, and active deal custom fields. Relationship values use pickers. Deal ID and immutable ID transitions are hidden.

| Trigger key | Built-in behavior | Up-front selection | Special field behavior |
| --- | --- | --- | --- |
| `crm.deal.created` | A new durable deal is created | None | Current values only; forward-looking note |
| `crm.deal.updated` | Any selected changed field, or any business field when watch is off | Optional specific deals | Show watch selector and Current/Previous/New groups; exclude ID and creator from watch |
| `crm.deal.deleted` | Deal is deleted; event contains the pre-delete business snapshot | Optional specific deals | Current group is labelled **Value before deletion** |
| `crm.deal.created_or_updated` | Create always; update obeys optional watch | None | Current values only; explicit combined-event note |
| `crm.deal.stage_changed` | Stage changed | Optional specific deals | Previous stage and New stage available |
| `crm.deal.entered_stage` | Stage changed and New stage equals configured stage | Optional specific deals | Required stage selector; Previous stage available; fixed New stage hidden |
| `crm.deal.exited_stage` | Stage changed and Previous stage equals configured stage | Optional specific deals | Required stage selector; New stage available; fixed Previous stage hidden |
| `crm.deal.won` | Status changed to Won | Optional specific deals | Previous status available; fixed current/new status hidden |
| `crm.deal.lost` | Status changed to Lost | Optional specific deals | Previous status available; fixed current/new status hidden |
| `crm.deal.reopened` | Status changed to Open | Optional specific deals | Previous status available; fixed current/new status hidden |
| `crm.deal.amount_changed` | Amount changed | Optional specific deals | Previous amount and New amount use decimal editors |
| `crm.deal.expected_close_date_changed` | Expected close date changed | Optional specific deals | Previous and New close date use timezone-aware datetime editors |
| `crm.deal.owner_id_changed` | Owner changed | Optional specific deals | Previous/New owner pickers plus personal post-update scope warning |
| `crm.deal.account_id_changed` | Related account changed | Optional specific deals | Previous/New account pickers |
| `crm.deal.primary_contact_id_changed` | Primary contact changed | Optional specific deals | Previous/New contact pickers |
| `crm.deal.amount_crossed` | Amount crossed configured threshold up or down | Optional specific deals | Required direction and decimal threshold; amount conditions hidden to prevent contradiction |

Deal field controls:

| Business field | Control/source |
| --- | --- |
| Pipeline | Pipeline select |
| Stage | Pipeline-grouped stage select |
| Status | Bounded deal-status select |
| Currency | Active distinct-value select; disable with reason when empty |
| Owner / Created by | CRM user picker with **Me** shortcut when resolvable |
| Account | Account search and batch hydration |
| Primary contact | Contact search and batch hydration |
| Custom select/multiselect | Current custom-field option labels |
| Custom relation-free scalar | Typed scalar editor from CRM schema |

### 8.2 Contact — 9 definitions

Personal scope is **Contacts currently assigned to you**. It is not “owned or created.” Organisation scope is **All organisation contacts**. Unassigned contacts do not activate personal triggers.

Standard fields are first name, last name, email, phone, owner, account, and lifecycle. Contact ID is hidden. In personal mode, Current owner and New owner are also hidden as authority-determined; Previous owner remains useful for ownership transitions.

| Trigger key | Built-in behavior | Up-front selection | Special field behavior |
| --- | --- | --- | --- |
| `crm.contact.created` | Contact created | None | Current values only |
| `crm.contact.updated` | Optional selected-field watch | Optional specific contacts | Current/Previous/New; immutable ID excluded |
| `crm.contact.deleted` | Contact deleted with pre-delete snapshot | Optional specific contacts | Value-before-deletion group |
| `crm.contact.created_or_updated` | Create always; update obeys watch | None | Current values only |
| `crm.contact.email_changed` | Email changed | Optional specific contacts | Previous/New email editors |
| `crm.contact.phone_changed` | Phone changed | Optional specific contacts | Previous/New phone editors |
| `crm.contact.owner_id_changed` | Owner changed | Optional specific contacts | Previous/New CRM user pickers; personal trigger belongs to new owner |
| `crm.contact.account_id_changed` | Account changed | Optional specific contacts | Previous/New account pickers |
| `crm.contact.lifecycle_stage_changed` | Lifecycle changed | Optional specific contacts | Previous/New controlled lifecycle values |

Lifecycle values come from tenant data or provider schema. If no controlled values can be resolved, hide/disable the field; never ask for an internal code or ID.

### 8.3 Account — 8 currently advertised, 7 released in this tranche

Personal scope is **Accounts currently assigned to you**. Organisation scope is **All organisation accounts**. Unassigned accounts do not activate personal triggers.

Standard fields are account name, domain, owner, lifecycle, tier, and annual revenue. Account ID is hidden. In personal mode, Current owner and New owner are authority-determined and hidden; Previous owner remains useful for ownership transitions.

| Trigger key | Built-in behavior | Up-front selection | Special field behavior |
| --- | --- | --- | --- |
| `crm.account.created` | Account created | None | Current values only |
| `crm.account.updated` | Optional selected-field watch | Optional specific accounts | Current/Previous/New; immutable ID excluded |
| `crm.account.deleted` | Account deleted with pre-delete snapshot | Optional specific accounts | Value-before-deletion group |
| `crm.account.created_or_updated` | Create always; update obeys watch | None | Current values only |
| `crm.account.domain_changed` | Domain changed | Optional specific accounts | Previous/New domain inputs |
| `crm.account.owner_id_changed` | Owner changed | Optional specific accounts | Previous/New CRM user pickers; personal trigger belongs to new owner |
| `crm.account.lifecycle_stage_changed` | Lifecycle changed | Optional specific accounts | Previous/New controlled lifecycle values |
| `crm.account.tier_changed` | Currently has no proven writer and is marked unavailable | Not shown | Re-enable only with a separately tested producer path |

### 8.4 Email — 2 definitions

Personal scope is **New messages in active mailboxes connected by you**. Organisation scope is **New messages in all active organisation mailboxes**. Both are forward-looking; initial sync/backfill does not run agents. Drafts do not emit. An inbound reply is part of Email received because no separate Email replied producer exists.

| Trigger key | Mailbox selector | Standard conditions | Hidden conditions |
| --- | --- | --- | --- |
| `email.message.received` | All in authority by default; optional specific mailboxes | Sender, sender domain, recipients, has attachments, related deal | Email ID, content ID, direction, thread ID, mailbox owner, actor/source metadata |
| `email.message.sent` | All in authority by default; optional specific mailboxes | Recipients, has attachments, related deal | Sender and sender domain are redundant with mailbox selection; all technical IDs and direction hidden |

Mailbox labels use normalized email plus provider. Recipient fields combine To, CC, and BCC because that is the durable event shape; the UI must say **Any To/CC/BCC recipient** rather than imply separate recipient lists.

### 8.5 Meeting — 2 definitions

Personal scope is **Meetings where you are the resolved internal organiser; otherwise the connected-calendar owner**. Internal attendees do not gain personal activation authority. Organisation scope is **All organisation meetings**.

| Trigger key | Timing | Standard conditions | Hidden conditions |
| --- | --- | --- | --- |
| `meeting.transcript.ready` | Durable transcript becomes ready after binding activation | Provider, transcript language, internal attendees, related deal; organiser only in organisation mode | Meeting IDs, actor/source metadata, CRM team IDs |
| `meeting.summary.ready` | Durable canonical summary becomes ready after binding activation | Provider, language, internal attendees, related deal; organiser only in organisation mode | Meeting IDs, actor/source metadata, CRM team IDs |

`attendee_ids` contains active internal workspace users, not external guests. `team_ids` currently has no reliable team-name resolver and must remain hidden until one exists. Do not label it as a usable Teams or CRM-team picker.

### 8.6 AI deal risk and blocker — 6 definitions

All six are **Run as organisation only**, so only eligible admins may save them. Risk and Blocker remain separate catalog resources even though their subject is a deal.

All six may optionally narrow to selected deals. Technical signal/evidence/model/prompt/inference IDs are hidden. `contact_id` is currently always null and **Buyer must not be offered** until the producer supplies it.

| Trigger key | Standard conditions |
| --- | --- |
| `ai.deal_risk.detected` | Severity and confidence; status is fixed Open and hidden |
| `ai.deal_risk.changed` | Current status, severity, and confidence |
| `ai.deal_risk.resolved` | Resolution status (Resolved, Dismissed, Won't fix), severity, and confidence |
| `ai.deal_blocker.detected` | Severity and confidence; status is fixed Open and hidden |
| `ai.deal_blocker.changed` | Current status, severity, and confidence |
| `ai.deal_blocker.resolved` | Resolution status (Resolved, Dismissed, Won't fix), severity, and confidence |

These are signal events, not update events. They do not contain previous/new change pairs. The Changed screen must not advertise transition filters.

### 8.7 Agent run — 2 definitions

Personal scope is **Runs created under your user identity**. Organisation scope is **All organisation runs**. The optional source selector is **Agents**, defaulting to all agents in scope; do not silently preselect the agent currently being edited because monitoring other agents is a valid use case.

| Trigger key | Standard conditions | Hidden/redundant conditions |
| --- | --- | --- |
| `agent.run.completed` | Triggering automation, causing event; Created by only in organisation mode | Run ID; status is always Succeeded and hidden |
| `agent.run.failed` | Failure type, triggering automation, causing event; Created by only in organisation mode | Run ID |

Friendly failed statuses are **Execution failed**, **Delivery failed**, and **Output validation failed**. A cancelled run is not selectable because `agent.run.cancelled` is not producer-backed in the catalog. Causation-depth and repeated-trigger protections remain invisible runtime safety controls; saved UI copy may say **Repeated automation loops are suppressed**.

### 8.8 Manual — 1 definition

`system.manual.invoked` saves a reusable binding and has no optional filter fields. Saving it does not run the agent.

**Run now** remains a separate flow:

- one global run; or
- selected Deals, Contacts, or Accounts, up to the catalog limit;
- personal record selection follows the exact Deal/Contact/Account scope in section 4;
- organisation record selection is admin-only and tenant-scoped.

Selected record chips require search, batch hydration, and unavailable-value behavior from section 6.

### 8.9 Schedule — 1 definition

`time.schedule.due` has configuration, not optional event filters:

- searchable IANA timezone;
- start date/time interpreted in that timezone;
- one-time, daily, weekdays, weekly, biweekly, monthly, or custom interval;
- optional record type and selected Deal/Contact/Account values;
- no selected records means one global run per occurrence;
- selected records mean one run per record per occurrence;
- if a selected record is unavailable at due time, the whole schedule pauses rather than partially fanning out;
- a completed one-time schedule is derived as `status=active` plus `next_trigger_at=null` and displays **Completed**.

Scheduled time and timezone are hidden from Optional conditions because Configuration already determines them.

### 8.10 Webhook — 1 definition

`integration.webhook.received` has a payload-schema configuration and no optional filters after technical fields are hidden. The current catalog does not generate matchable fields from the user JSON Schema, so the UI must not imply payload-property filtering.

The form explains:

- personal webhook executes as the saved user;
- organisation webhook is admin-only and executes as the organisation service;
- possession of the URL and signing secret is bearer-like execution authority;
- credentials appear only once after create or rotate;
- secret rotation does not change trigger semantics;
- request size is bounded by the catalog limit.

---

## 9. Field-removal and replacement matrix

“Remove” below means remove from normal business-user controls, not delete from durable envelopes or matcher compatibility.

| Current field/path family | Decision | Replacement |
| --- | --- | --- |
| `record.id` | Hidden | Up-front record selector where meaningful |
| `changes.id.before/after` | Hidden and non-watchable | None; identity cannot change |
| `actor.type` | Hidden | Built-in event/source description |
| `actor.id` | Hidden | Email mailbox selector compiles to it internally |
| `source.domain/system/operation/mode/source_event_id` | Hidden | Timing/source copy; diagnostics remain backend-only |
| Deal/Contact/Account owner IDs | Keep as business fields | CRM user picker and Me shortcut |
| Account/contact/deal relation IDs | Keep as business relationships | Async entity picker |
| Email ID/content ID/thread ID | Hidden | No control until a thread/content search use case exists |
| Email mailbox owner ID | Hidden | Authority scope and mailbox selector |
| Meeting ID/reference ID | Hidden | No meeting selector in this tranche |
| Meeting team IDs | Hidden | Wait for a reliable team-name resolver |
| AI subject/signal/evidence/model/prompt/inference IDs | Hidden | Deal selector plus severity/status/confidence |
| AI Buyer/contact ID | Hidden | Producer currently supplies null |
| Agent run ID | Hidden | Agent/trigger/event label controls |
| Agent/trigger/user relationship IDs | Keep as business concepts | Accessible-agent, saved-trigger, and workspace-user pickers |
| Schedule attributes | Hidden from filters | Schedule Configuration section |
| Webhook dedupe key and source metadata | Hidden | Webhook delivery diagnostics |

---

## 10. Implementation plan

### Gate 0: Create a clean baseline checkpoint

**Files:** No implementation files from this plan

- [ ] Run the existing focused backend automation suite:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/automation tests/api/routers/automation -q
```

Expected: exit 0 with no failed tests.

- [ ] Run the existing frontend Agent V1 unit tests and type check:

```powershell
yarn test:jest __tests__/unit/modules/agents-v1 --runInBand
yarn check-types
```

Expected: both commands exit 0.

- [ ] Review `git status --short`, `git diff --cached --stat`, and `git diff --cached --check` in both repositories. Stage only the already-reviewed trigger baseline. Preserve unrelated worktree changes.

- [ ] Commit the baseline and this plan separately so every later vertical slice can be reverted independently. Do not push or deploy without separate authorization.

### Task 1: Make trigger detail round-trip user decisions, not compiled presets

**Backend files:**

- Modify: `src/service/automation/triggers/matcher.py`
- Modify: `src/service/automation/triggers/serializers.py`
- Modify: `src/api/schemas/automation_trigger_models.py`
- Modify: `src/service/automation/triggers/service.py`
- Test: `tests/service/automation/triggers/test_serializers.py`
- Test: `tests/service/automation/triggers/test_service.py`
- Test: `tests/service/automation/triggers/test_matcher_validation.py`

- [ ] Add failing tests proving:

  - Deal entered-stage detail returns `watch=null` and `filter=null` when the user added neither;
  - a semantic fixed filter plus one user condition returns only that user condition;
  - GET-detail to PUT-replace does not duplicate fixed filters;
  - an unseparable stored matcher becomes `needs_attention` and is not guessed.

- [ ] Add `extract_user_filter()` exactly as defined in section 5.5 and use the current registry/compiler to derive the concrete fixed filter from `trigger_config`.

- [ ] Serialize user watch only when `definition.allow_user_watch` is true.

- [ ] Pass the request-scoped registry snapshot into detail serialization so tenant custom-field definitions remain authoritative.

- [ ] Run:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/automation/triggers/test_matcher_validation.py tests/service/automation/triggers/test_serializers.py tests/service/automation/triggers/test_service.py -q
```

Expected: exit 0.

- [ ] Commit: `fix(automation): round-trip user trigger decisions`

### Task 2: Replace the current catalog with the business-facing contract

**Backend files:**

- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/catalog.py`
- Modify: `src/service/automation/triggers/validator.py`
- Modify: `src/service/automation/triggers/service.py`
- Modify: `src/api/schemas/automation_trigger_models.py`
- Modify: `src/api/routers/automation/triggers_handler.py`
- Modify: `docs/API Documentation/automation/v1-agent-triggers-api.md`
- Test: `tests/service/automation/triggers/test_catalog.py`
- Test: `tests/service/automation/triggers/test_validator.py`
- Test: `tests/api/routers/automation/test_trigger_contracts.py`

- [ ] Add failing tests for the exact scope copy, structured watch labels, business-rule summaries, timing notes, field groups, principal-mode visibility, hidden technical paths, and the single returned contract.

- [ ] Add registry presentation metadata and `RecordField.watchable`. Keep matching/accessors unchanged.

- [ ] Replace presentation use of the current field/watch shape; serialize hidden fields in the one returned catalog.

- [ ] Reject hidden or unrecognized paths on create and replace. Do not add a preservation branch for the replaced development contract.

- [ ] Keep one unversioned catalog endpoint and one write contract.

- [ ] Add a registry/catalog matrix test that recognizes all 48 current keys, asserts `crm.account.tier_changed` is unavailable, and returns the other 47 exactly once.

- [ ] Run:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/automation/triggers/test_catalog.py tests/service/automation/triggers/test_validator.py tests/api/routers/automation/test_trigger_contracts.py -q
```

Expected: exit 0 and the returned catalog matrix reports 47 definitions.

- [ ] Commit: `feat(automation): add business trigger catalog`

### Task 3: Add authority-aware source selection and option resolution

**Backend files:**

- Modify: `src/api/schemas/automation_trigger_models.py`
- Modify: `src/api/routers/automation/triggers_handler.py`
- Modify: `src/service/automation/triggers/options.py`
- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/validator.py`
- Modify: `src/service/automation/triggers/activation.py`
- Modify: `src/service/automation/triggers/service.py`
- Add test: `tests/service/automation/triggers/test_options.py`
- Test: `tests/service/automation/triggers/test_validator.py`
- Test: `tests/service/automation/triggers/test_activation_service.py`
- Test: `tests/api/routers/automation/test_trigger_contracts.py`

- [x] Add failing security tests for personal versus organisation Deal/Contact/Account/mailbox selection, non-admin organisation rejection, foreign IDs, inactive IDs, over-limit IDs, and filter relations that are tenant-valid but not personally owned.

- [x] Add failing API tests for search, pagination, batch resolution, renamed labels, inactive/deleted tombstones, foreign-value non-disclosure, and the 200-value total cap.

- [x] Treat omitted selection or the catalog-provided All default identically and compile selected IDs to one fixed `in` leaf. Do not introduce a second config version for this development-only change.

- [x] Move authority resolution before source-selection validation. Keep ordinary option validation tenant-scoped.

- [x] Compile Email mailbox IDs onto existing `actor.id`; do not modify `EmailEventAdapter` or the envelope schema.

- [x] During activation, validate the saved config shape and evaluate stable IDs against the event snapshot. Do not require a selected CRM row to remain active before matching its deletion event.

- [x] Run:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/automation/triggers/test_options.py tests/service/automation/triggers/test_validator.py tests/service/automation/triggers/test_activation_service.py tests/api/routers/automation/test_trigger_contracts.py -q
```

Expected: exit 0.

- [x] Commit: `feat(automation): add scoped trigger source selectors`

### Task 4: Build the fail-closed frontend catalog and typed editor foundation

**Frontend files:**

- Modify: `src/modules/agents-v1/api/agents-v1.api.ts`
- Modify: `src/modules/agents-v1/api/agents-v1.client.ts`
- Modify: `src/modules/agents-v1/hooks/agents-v1-query-keys.ts`
- Modify: `src/modules/agents-v1/hooks/use-agent-automation.ts`
- Modify: `src/modules/agents-v1/types/agents-v1.types.ts`
- Modify: `src/modules/agents-v1/types/agents-v1.schemas.ts`
- Modify: `src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Test: `__tests__/unit/modules/agents-v1/agents-v1.api.test.ts`
- Test: `__tests__/unit/modules/agents-v1/agents-v1.schemas.test.ts`
- Test: `__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`

- [ ] Add failing tests that the client consumes the single catalog contract, never sends version-negotiation fields, never offers hidden fields in Add condition, never shows raw watch keys, and disables unknown input/option sources rather than falling back to text.

- [ ] Replace comma-separated editors with the operator/type matrix in section 7.1.

- [ ] Render groups from catalog `group`; do not parse field paths in React.

- [ ] Fail closed on hidden or unrecognized saved paths; do not add a compatibility-condition editor.

- [ ] Implement debounced option search and batch selected-value resolution with skeleton chips and no raw-ID fallback.

- [ ] Preserve compatible form values on event change; announce every removed incompatible value before save.

- [ ] Run:

```powershell
yarn test:jest __tests__/unit/modules/agents-v1/agents-v1.api.test.ts __tests__/unit/modules/agents-v1/agents-v1.schemas.test.ts __tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx --runInBand
yarn check-types
```

Expected: both commands exit 0.

- [ ] Commit: `feat(agents): add typed business trigger controls`

### Task 5: Deliver Deal as the first vertical slice

**Backend files:**

- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/catalog.py`
- Test: `tests/service/automation/triggers/test_catalog.py`
- Test: `tests/service/automation/triggers/test_validator.py`

**Frontend files:**

- Modify: `src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Test: `__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`
- Test: `__tests__/e2e/agents-v1.spec.ts`

- [ ] Add the exact 16-row matrix from section 8.1 as parameterized backend assertions.

- [ ] Add red tests proving native CopilotGTM Deal create/update/delete facts each create one registry-valid dispatch.

- [ ] Add backend and frontend assertions that this tranche describes CRM triggers as **Changes recorded in CopilotGTM**. Do not modify HubSpot/Salesforce writers in this plan.

- [ ] Add UI tests for locked **Deals you own or created**, no default Owner=me filter, owner/creator Me shortcuts, no selector on Created/Created-or-updated, selectors on update events, truthful combined-event copy, and semantic fixed-field hiding.

- [ ] Add browser coverage that saves and edits Deal Created, Updated with watch, Entered stage, Owner changed, and Amount crossed without displaying a UUID.

- [ ] Run:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/automation/triggers/test_catalog.py tests/service/automation/triggers/test_validator.py -q
yarn test:jest __tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx --runInBand
yarn test:e2e __tests__/e2e/agents-v1.spec.ts --project=chromium
```

Expected: all commands exit 0.

- [ ] Commit: `feat(agents): make deal triggers business friendly`

### Task 6: Deliver Email mailbox scope and address conditions

**Backend files:**

- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/options.py`
- Test: `tests/service/automation/triggers/test_catalog.py`
- Test: `tests/service/automation/triggers/test_options.py`
- Test: `tests/service/automation/triggers/test_validator.py`

**Frontend files:**

- Modify: `src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Test: `__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`
- Test: `__tests__/e2e/agents-v1.spec.ts`

- [ ] Test personal mailbox queries return only active mailboxes connected by the caller and organisation queries require admin and return all active organisation mailboxes.

- [ ] Test received/sent field visibility exactly matches section 8.4 and that mailbox selection compiles to `actor.id`.

- [ ] Test normalized sender and recipient tokens, multi-recipient operators, attachment Yes/No, related-deal hydration, one-mailbox and many-mailbox states, disconnected mailbox tombstones, and the forward-looking initial-sync note.

- [ ] Assert no response or rendered text contains `record.mailbox_integration_id` because that field must not be added.

- [ ] Run the focused backend/frontend commands from Task 5 with the email option/producer tests included.

- [ ] Commit: `feat(agents): add friendly email trigger scope`

### Task 7: Deliver Contact and Account

**Backend files:**

- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/options.py`
- Test: `tests/service/automation/triggers/test_catalog.py`
- Test: `tests/service/automation/triggers/test_validator.py`

**Frontend files:**

- Modify: `src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Test: `__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`
- Test: `__tests__/e2e/agents-v1.spec.ts`

- [ ] Parameterize the exact 9 Contact rows and the 7 available Account rows from sections 8.2 and 8.3; separately assert Tier changed is unavailable.

- [ ] Prove native CopilotGTM Contact and Account mutations emit one event per changed record and deletion uses the pre-delete business snapshot. Provider emission remains outside this plan.

- [ ] Test personal scope copy says **currently assigned to you**, never **created by you**.

- [ ] Test owner-change post-update warnings, record selectors, lifecycle/tier controlled values, unavailable option behavior, relation hydration, and pre-delete snapshot wording.

- [ ] Run focused backend catalog/validator tests, frontend trigger-builder tests, type check, and Contact/Account browser paths.

- [ ] Commit: `feat(agents): add friendly contact and account triggers`

### Task 8: Deliver Meeting, AI, and Agent run

**Backend files:**

- Modify: `src/service/automation/triggers/registry.py`
- Modify: `src/service/automation/triggers/options.py`
- Test: `tests/service/automation/triggers/test_catalog.py`
- Test: `tests/service/automation/triggers/test_options.py`

**Frontend files:**

- Modify: `src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Test: `__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`
- Test: `__tests__/e2e/agents-v1.spec.ts`

- [ ] Test Meeting organiser authority copy, internal-attendee labels, organiser visibility by principal mode, hidden team IDs, and no implication that external attendees are selectable.

- [ ] Test all six AI keys are organisation-only, Risk/Blocker resources do not collide, Buyer is hidden, Detected/Changed/Resolved fields match section 8.6, and no previous/new groups appear.

- [ ] Test Agent-run all-agent default, optional accessible-agent selection, Completed status hiding, Failed status options, causing-event labels, trigger hydration, and Created-by visibility only in organisation mode.

- [ ] Run focused backend/frontend tests, type check, and the three source browser paths.

- [ ] Commit: `feat(agents): finish signal trigger controls`

### Task 9: Finish Manual, Schedule, Webhook, audience, and saved summaries

**Backend files:**

- Modify: `src/service/automation/triggers/options.py`
- Modify: `src/service/automation/triggers/serializers.py`
- Modify: `src/service/automation/triggers/service.py`
- Modify: `src/api/schemas/automation_trigger_models.py`
- Test: `tests/service/automation/triggers/test_direct.py`
- Test: `tests/service/automation/triggers/test_schedule.py`
- Test: `tests/service/automation/triggers/test_webhook.py`
- Test: `tests/service/automation/triggers/test_serializers.py`

**Frontend files:**

- Modify: `src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Modify: `src/modules/agents-v1/components/agent-detail-page.tsx`
- Test: `__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`
- Test: `__tests__/unit/modules/agents-v1/agent-detail-page.test.tsx`
- Test: `__tests__/e2e/agents-v1.spec.ts`

- [ ] Test Manual create versus invoke separation and hydrated direct-record chips.

- [ ] Test Schedule global versus fan-out behavior, exact personal record scope, IANA timezone, completed one-time state, and whole-schedule pause on an unavailable due-time target.

- [ ] Test Webhook has no filter editor, shows credential/authority warnings, and retains one-time secret handling.

- [ ] Reuse active workspace-member options for explicit organisation audience. Test that members cannot submit organisation mode and that audience never changes source options or matching.

- [ ] Build the batched `AutomationTriggerPresentation` contract from section 6.7 and render saved cards with friendly breadcrumb, scope badge, built-in rule, source-selection summary, condition summary, hydrated labels, and Needs-attention tombstones. Never display raw `trigger_key`, matcher path, or stable ID as the primary label.

- [ ] Commit: `feat(agents): complete trigger management presentation`

### Task 10: Cross-trigger verification and development promotion gate

**Backend test files:**

- Add: `tests/service/automation/triggers/test_catalog_execution_matrix.py`

**Frontend test files:**

- Modify: `__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`
- Modify: `__tests__/e2e/agents-v1.spec.ts`

- [ ] Add a parameterized matrix for all 47 returned definitions asserting producer availability, catalog metadata, supported principal modes, source selector policy, standard/hidden fields, and a valid minimal decision. Assert Tier changed remains absent.

- [ ] For every enabled event definition, run producer fixture → durable dispatch → activation → claimable AgentRun in backend integration tests. Direct Manual/Schedule/Webhook use their direct-route fixtures.

- [ ] Run the full focused backend trigger suite:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/automation tests/api/routers/automation tests/workers/chat_worker -q
```

- [ ] Run frontend verification:

```powershell
yarn test:jest __tests__/unit/modules/agents-v1 --runInBand
yarn check-types
yarn lint
yarn test:e2e __tests__/e2e/agents-v1.spec.ts --project=chromium
```

- [ ] Browser-test as four identities/states: member-personal, member-organisation-denied, admin-personal, and admin-organisation. Cover create, edit, pause, activate, archive, manual invoke, schedule completed, webhook rotate, renamed option, deleted option, and zero-options state.

- [ ] Run `git diff --check` in both repositories and review staged file lists before each final commit.

- [ ] Replace the backend and frontend development contract together. Recreate disposable development trigger rows, verify the one catalog response and one write shape end to end, and do not promote the feature while either side still references a version-negotiation field.

---

## 11. Acceptance criteria

1. Deal / Created + Run as me automatically shows a locked **Deals you own or created** scope and saves no Owner=me condition.
2. Contact and Account personal copy says currently assigned/owned, never created by.
3. Email personal mode defaults to all active mailboxes connected by the user; organisation mode defaults to all active organisation mailboxes.
4. Only admins can select or submit Run as organisation; stale-client submissions are rejected by the backend.
5. No business-user flow asks the user to type or recognize a UUID, event key, matcher path, integration ID, run ID, or source event ID.
6. Search, loading, edit, saved cards, inactive values, and deleted values never flash raw IDs.
7. Every entity relationship uses searchable labels and batch hydration.
8. Unknown editor or option metadata fails closed and never becomes a generic text input.
9. Hidden technical fields remain catalog-recognizable as backend-owned paths but cannot be authored.
10. Hidden or unrecognized condition paths fail closed and never appear as editable or raw-ID controls.
11. User detail/replace round trips do not duplicate fixed filters or resubmit fixed watches as user decisions.
12. Watch selectors show friendly labels and exclude immutable IDs and creator provenance.
13. Created-or-updated copy states that creates always run and watch selection gates updates only.
14. Created-or-updated offers Current values only and no specific-record selector.
15. Semantic fixed rules use business sentences and cannot be contradicted through offered fields.
16. Deleted CRM triggers describe and match the pre-delete business snapshot.
17. Email mailbox narrowing reuses `actor.id`; no duplicate mailbox field or event schema change is introduced.
18. Email received distinguishes new inbound mail from imported history and explains that inbound replies are included.
19. Email sent hides redundant sender/direction fields and keeps recipient/attachment/deal conditions.
20. Meeting personal authority is organiser/fallback-calendar-owner, not attendee; external attendees are not presented as internal selectable users.
21. Meeting team IDs remain hidden until a label resolver exists.
22. AI Risk and Blocker are distinct resources, organisation-only, and never offer Buyer while the producer supplies null.
23. AI signal events never show previous/new fields.
24. Agent-run Completed hides its fixed Succeeded status; Failed exposes only the three produced failure statuses.
25. Manual creation and invocation remain separate.
26. Schedule configuration and optional filters are not duplicated; selected record labels survive edit.
27. Webhook exposes no filter editor and clearly describes one-time credentials and execution authority.
28. Audience controls affect visibility only and never change event matching.
29. The catalog contains exactly 47 producer-proven definitions; Account Tier changed remains unavailable.
30. An integration matrix covering every advertised definition proves each enabled path can reach a claimable AgentRun.
31. Every definition with source selection supplies an explicit All UI default; omission means All and malformed selection fails validation.
32. Exactly one catalog and write contract exists; no query/request version negotiation or compatibility branch remains.
33. No new dependency is added.
34. No trigger table is dropped or recreated for this work.
35. Every implementation slice has focused tests, type checking where applicable, whitespace verification, and an independently revertible commit.
36. Deal, Contact, and Account labels explicitly say **Changes recorded in CopilotGTM** until the separate HubSpot/Salesforce provider migration is delivered and verified.

---

## 12. Explicit non-goals

- No new event producers.
- No HubSpot/Salesforce writer, webhook, or CDC trigger integration; that work requires its own provider-migration/adapter plan.
- No initial-sync/backfill opt-in.
- No payload-property filters for Webhook.
- No meeting search/selector until an authorized meeting picker exists.
- No CRM team filter until team IDs have reliable tenant-scoped labels.
- No Buyer filter until AI signal events populate `contact_id`.
- No advanced drawer containing raw IDs.
- No frontend hardcoded trigger-key catalog or authority policy.
- No change to the event/outbox/run architecture.
- No production deployment or migration authorization in this document.

---

## 13. Final recommendation

Replace the current development trigger picker contract directly and implement this document in vertical slices. Do not first add Owner=me as a stored condition and repair it later. The correct first slice is the shared foundation: decision round-trip, the single business-facing catalog, authority-aware label resolution, and typed fail-closed editors. Deal and Email then become small, truthful applications of that foundation instead of one-off UI logic.
