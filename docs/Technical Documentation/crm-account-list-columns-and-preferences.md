# CRM Account List Columns and Preferences

> Date: 2026-06-22
> Status: Implemented 2026-06-23

## Goal

Bring the CRM accounts list closer to the recent contacts list pattern:

- Provide a backend-owned default ordered column list for the frontend.
- Return account list rows with enough sales context for table rendering.
- Preserve a future path for user-selected columns without adding a preference API now.
- Keep list payloads lightweight; detailed contacts and deals remain on account detail endpoints.

## Current Contact Pattern

Contacts now use `user_preferences.crm.contacts.list.columns` as the frontend column source.
The backend fills defaults during login/session bootstrap when the user does not already have
valid contact list column preferences.

Current contact defaults:

```python
[
    "full_name",
    "job_title",
    "company.name",
    "email",
    "phone",
    "linkedin_url",
    "owner.name",
    "deal_count",
    "last_contacted_at",
    "last_activity_at",
]
```

Contacts list also returns nested account and owner context:

```json
{
  "company": {
    "id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
    "name": "Acme Corp",
    "domain": "acme.com"
  },
  "owner": {
    "id": "9a8b7c6d-e5f4-4321-9876-123456789abc",
    "name": "Jane Smith",
    "email": "jane@example.com"
  }
}
```

Accounts should follow the same preference and nested-owner shape where practical.

## Current Account List Shape

`GET /users/crm/accounts` currently returns `AccountListItem` rows with flat owner data:

```json
{
  "company_id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
  "name": "Acme Corp",
  "domain": "acme.com",
  "website": "https://acme.com",
  "industry": "Technology",
  "lifecycle_stage": "customer",
  "owner_name": "Jane Smith",
  "number_of_employees": 250,
  "annual_revenue": 50000000.0,
  "phone": "+1-555-0100",
  "country": "US",
  "linkedin_url": "https://linkedin.com/company/acme-corp",
  "is_active": true,
  "contact_count": 12,
  "deal_count": 3,
  "pipeline_value": 150000.0,
  "created_at": "2026-01-10T08:00:00Z",
  "updated_at": "2026-03-20T14:00:00Z"
}
```

The strongest sales-facing fields already exist:

- `contact_count`: stakeholder/account coverage
- `deal_count`: active opportunity footprint
- `pipeline_value`: open pipeline value
- `industry`, `lifecycle_stage`, `number_of_employees`, `annual_revenue`: segmentation and fit
- `owner_name`: current owner display, but missing owner ID/email

## Proposed Account List Contract

Add nested `owner` while keeping list rows lightweight:

```json
{
  "company_id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
  "name": "Acme Corp",
  "domain": "acme.com",
  "website": "https://acme.com",
  "industry": "Technology",
  "lifecycle_stage": "customer",
  "owner": {
    "id": "9a8b7c6d-e5f4-4321-9876-123456789abc",
    "name": "Jane Smith",
    "email": "jane@example.com"
  },
  "owner_name": "Jane Smith",
  "number_of_employees": 250,
  "annual_revenue": 50000000.0,
  "phone": "+1-555-0100",
  "country": "US",
  "linkedin_url": "https://linkedin.com/company/acme-corp",
  "is_active": true,
  "contact_count": 12,
  "deal_count": 3,
  "pipeline_value": 150000.0,
  "created_at": "2026-01-10T08:00:00Z",
  "updated_at": "2026-03-20T14:00:00Z"
}
```

Keep `owner_name` temporarily for backwards compatibility. New frontend column mapping should use
`owner.name`.

Do not add nested `contacts` or `deals` to account list rows. The counts are enough for a table;
full related records belong in `GET /users/crm/accounts/{company_id}` and the child endpoints.

## Default Account Columns

Recommended backend-owned default:

```python
DEFAULT_ACCOUNT_LIST_COLUMNS = [
    "name",
    "domain",
    "industry",
    "lifecycle_stage",
    "owner.name",
    "contact_count",
    "deal_count",
    "pipeline_value",
    "number_of_employees",
    "annual_revenue",
    "country",
    "linkedin_url",
]
```

These are intentionally ordered from identity and ownership to sales activity and firmographics.

## Default Deal Columns

Deals also use backend-owned defaults from `user_preferences.crm.deals.list.columns`.
The current default list is intentionally CRM-friendly and excludes health,
forecast category, and probability as default table columns:

```python
DEFAULT_DEAL_LIST_COLUMNS = [
    "name",
    "company.name",
    "stage.name",
    "amount",
    "expected_close_date",
    "owner.name",
    "primary_contact.name",
    "last_activity_at",
    "deal_status",
    "pipeline.name",
]
```

`GET /users/crm/deals` returns all fields needed for these default columns:

| Column ID | Payload Path |
| --- | --- |
| `name` | `item.name` |
| `company.name` | `item.company_name` |
| `stage.name` | `item.stage_label ?? item.stage_name` |
| `amount` | `item.amount`, plus `item.currency` / `item.currency_symbol` |
| `expected_close_date` | `item.expected_close_date` |
| `owner.name` | `item.owner_details[0]?.name` |
| `primary_contact.name` | `item.primary_contact_name` |
| `last_activity_at` | `item.last_activity_at` |
| `deal_status` | `item.deal_status` |
| `pipeline.name` | `item.pipeline_label ?? item.pipeline_name` |

`probability` and `forecast_category` remain available on list rows for existing clients
and optional future columns, but they are not part of the backend default. `deal_health`
and `deal_health_reason` are not returned by the deal list API; they remain available on
deal detail only.

## Optional Columns

Expose these as selectable later, but do not include them by default:

```python
[
    "website",
    "phone",
    "is_active",
    "created_at",
    "updated_at",
]
```

Do not include these until the account model and API actually expose them:

```python
[
    "company_research",
    "research_status",
    "last_researched_at",
]
```

The accounts API documentation currently mentions research fields, but the current account model and
list schema do not expose those fields. Treat them as planned/stale documentation until implemented.

## Preference Shape

Extend the existing normalized preference object:

```json
{
  "crm": {
    "contacts": {
      "list": {
        "columns": [
          "full_name",
          "job_title",
          "company.name",
          "email"
        ]
      }
    },
    "accounts": {
      "list": {
        "columns": [
          "name",
          "domain",
          "industry",
          "lifecycle_stage",
          "owner.name",
          "contact_count",
          "deal_count",
          "pipeline_value",
          "number_of_employees",
          "annual_revenue",
          "country",
          "linkedin_url"
        ]
      }
    },
    "deals": {
      "list": {
        "columns": [
          "name",
          "company.name",
          "stage.name",
          "amount",
          "expected_close_date",
          "owner.name",
          "primary_contact.name",
          "last_activity_at",
          "deal_status",
          "pipeline.name"
        ]
      }
    }
  }
}
```

Normalization rule:

- If `crm.accounts.list.columns` is missing or invalid, set it to `DEFAULT_ACCOUNT_LIST_COLUMNS`.
- If `crm.deals.list.columns` is missing or invalid, set it to `DEFAULT_DEAL_LIST_COLUMNS`.
- If it is a list of strings, preserve it exactly.
- Do not validate against allowed column names at normalization time unless the contact preference
  behavior is also tightened. Keep behavior consistent across CRM tables.

## Backend Implementation Plan

1. Update `src/service/users/user_preferences.py`.
   - Add `DEFAULT_ACCOUNT_LIST_COLUMNS`.
   - Extend `normalize_user_preferences()` to fill `crm.accounts.list.columns`.
   - Preserve existing contact behavior.

2. Update account schemas in `src/api/schemas/crm_account_models.py`.
   - Add `AccountListOwner`:

```python
class AccountListOwner(BaseModel):
    id: UUID | None
    name: str | None
    email: str | None
```

   - Add `owner: AccountListOwner | None` to `AccountListItem`.
   - Keep `owner_name` for compatibility.

3. Update `AccountReadService.list_accounts()`.
   - Build owner payload from `CRMCompany.owner_id`, `owner_name`, and `owner_email`.
   - Keep all queries filtered by `organisation_id` and `is_deleted = false`.

4. Update `src/api/routers/crm/accounts_handler.py`.
   - Pass through `owner` when constructing `AccountListItem`.
   - Document that relationship counters remain zero in mutation responses.

5. Update docs after implementation.
   - Update `docs/API Documentation/deals/crm-accounts-api.md`.
   - Update `docs/API Documentation/users-auth-api.md` with account list preference defaults.

## Suggested Tests

Add focused regression tests:

- `normalize_user_preferences({})` adds both contact and account default list columns.
- Existing `crm.accounts.list.columns` is preserved when it is a list of strings.
- Invalid `crm.accounts.list.columns` falls back to defaults.
- `GET /users/crm/accounts` returns nested `owner` with `id`, `name`, `email`.
- Existing `owner_name` remains in the response while the frontend migrates to `owner.name`.

## Frontend Mapping Guidance

Column IDs should map directly to payload paths:

| Column ID | Payload Path |
| --- | --- |
| `name` | `item.name` |
| `domain` | `item.domain` |
| `industry` | `item.industry` |
| `lifecycle_stage` | `item.lifecycle_stage` |
| `owner.name` | `item.owner?.name` |
| `contact_count` | `item.contact_count` |
| `deal_count` | `item.deal_count` |
| `pipeline_value` | `item.pipeline_value` |
| `number_of_employees` | `item.number_of_employees` |
| `annual_revenue` | `item.annual_revenue` |
| `country` | `item.country` |
| `linkedin_url` | `item.linkedin_url` |
Use `owner_name` only as fallback while the backend and frontend rollout is in progress:

```ts
const ownerName = account.owner?.name ?? account.owner_name;
```

## Rollout Notes

- This is additive if `owner_name` is retained, so existing frontend code should not break.
- Account preferences can ship before a dedicated preference update API because login/session
  bootstrap already returns normalized defaults.
- Once frontend fully uses `owner.name`, `owner_name` can be deprecated in a later API cleanup.
