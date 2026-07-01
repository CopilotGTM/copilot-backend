# Minimal Stakeholder Add Plan

## Goal

Let users add a deal stakeholder from one input, matching the contact create flow:

- email
- LinkedIn URL
- name

The UI should not ask for the current 10-15 stakeholder fields up front. Role, stance,
title, motivation, and similar fields stay editable after creation.

## Current State

- `POST /users/crm/deals/{deal_id}/stakeholders` uses `CreateStakeholderRequest` from
  `src/api/schemas/crm_models.py`, then converts to `CreateStakeholderCommand` in
  `src/contracts/crm/stakeholders.py`.
- `StakeholderWriteService.create_stakeholder()` requires `request.email`.
- Created stakeholders are `CRMDealParticipant` rows with `participant_type=CONTACT`.
- `StakeholderWriteService._ensure_contact_for_stakeholder()` creates or reuses a
  `CRMContact` only from email.
- Contact creation already resolves/creates account context from email domain through
  `ContactWriteService` and `AccountWriteService`.
- Contact create search already has a strict person-identity path:
  `ContactReadService.search_contacts_for_create()`.
- Agent tools that need updates:
  - `src/agents/tools/crm/add_stakeholder.py`
  - `src/agents/tools/crm/upsert_stakeholder.py`

## Product Rules

1. A stakeholder can be captured with name, email, or LinkedIn URL.
2. Email is the strongest identifier.
3. LinkedIn URL is the next strongest identifier.
4. Name alone is allowed, but it is not enough to run domain-based automation.
5. Name-only stakeholders must be visibly incomplete until email or LinkedIn is added.
6. Name-only creation should not create a durable `CRMContact` yet.
7. When email or LinkedIn is added later, resolve against existing contacts before creating
   a new contact.

## Proposed Backend Contract

Replace the email-required create payload with one primary input plus optional parsed fields:

```python
class CreateStakeholderCommand(BaseModel):
    identity: str
    name: str | None = None
    email: str | None = None
    linkedin_url: str | None = None
    ...
```

`identity` is the raw single-box user input. The service parses it into one of:

- `email`
- `linkedin_url`
- `name`

Keep `email`, `name`, and `linkedin_url` accepted for backward compatibility during rollout.
If `identity` is absent, derive it from the first non-empty value in `email`, `linkedin_url`,
`name`.

## Create Flow

### Email input

1. Normalize email.
2. Reject internal team member email as today.
3. Search existing contact by email.
4. Reuse or create contact through `ContactWriteService.find_or_create_contact_by_email()`.
5. Create/reactivate the deal participant.
6. Queue stakeholder processing and stakeholder research as today.

### LinkedIn input

1. Normalize LinkedIn URL.
2. Search existing contacts by exact `linkedin_url`.
3. If found, create/reactivate the deal participant linked to that contact.
4. If not found, create a stakeholder participant with `linkedin_url` and optional name.
5. Queue stakeholder research/enrichment; if enrichment returns email, resolve/create contact
   then link the participant to that contact.

### Name input

1. Normalize name.
2. Use existing strict contact candidate search in the deal/account context.
3. If the user selected a known contact, add that contact as stakeholder.
4. If no contact is selected, create a participant placeholder:
   - `related_name = name`
   - `related_email = None`
   - `related_local_id = None`
   - `approval_status = APPROVED`
   - `participant_source = MANUAL`
5. Do not create a `CRMContact`.
6. Do not run meeting/email/domain matching until email or LinkedIn exists.

## Data Model Impact

`CRMDealParticipant.related_email` is already nullable in the model and base org migration,
so name-only placeholders do not need a database migration.

No new table is needed. The placeholder is still the deal-level stakeholder row.

## Dedupe Rules

On create, check active and deleted participants in this order:

1. `related_local_id` when a contact is resolved.
2. normalized `related_email` when email exists.
3. normalized `linkedin_url` when LinkedIn exists.
4. normalized `related_name` only within the same deal, and only as a soft warning.

Name-only duplicates should not hard-conflict because names are weak identifiers. The API
can return candidates or a warning, but should still allow the placeholder when the user
confirms.

When a placeholder later gets email or LinkedIn:

1. Search existing contacts.
2. If a contact exists, link the participant to it.
3. If another stakeholder participant already exists on the same deal for that contact/email,
   merge the placeholder fields into the existing participant and delete/remove the placeholder.
4. If no contact exists and email is available, create the contact.

## API Changes

Update:

- `src/api/schemas/crm_models.py::CreateStakeholderRequest`
- `src/contracts/crm/stakeholders.py::CreateStakeholderCommand`
- `src/service/crm/participants/stakeholder_write_service.py::create_stakeholder`
- `src/service/crm/participants/stakeholder_adapter.py` if response serialization assumes
  email is always present.

Response shape should keep `StakeholderDetail`, but `email` needs to become nullable if
name-only placeholders are returned.

## Tool Changes

### `add_stakeholder`

Change the tool from email-required to one-input:

- add `identity: str | None`
- keep `email`, `name`, `linkedin_url` for compatibility
- update the tool description so the agent can call it with name, email, or LinkedIn
- stop asking the user for email when name or LinkedIn was already provided

### `upsert_stakeholder`

Change the upsert key:

- accept `identity`
- resolve identity type
- update by email when email exists
- update by LinkedIn when LinkedIn exists
- for name-only, create or update a placeholder on the target deal

## Context Search

Use the existing contact create-candidate search pattern rather than a new search system:

- email/name candidate search: `ContactReadService.search_contacts_for_create()`
- deal/account narrowing should use existing deal context where available
- LinkedIn exact search likely needs a small helper on `ContactReadService`

## Tests

Smallest useful checks:

1. API schema accepts `identity="buyer@example.com"` and maps to email.
2. API schema accepts `identity="https://linkedin.com/in/buyer"` and maps to LinkedIn.
3. API schema accepts `identity="Jane Buyer"` without email.
4. `StakeholderWriteService.create_stakeholder()` creates a name-only participant without a
   contact.
5. Email create still creates/reuses contact and queues normal jobs.
6. Tool tests cover `add_stakeholder` no longer rejecting name-only input.

## Rollout Order

1. Make response and command contracts nullable/compatible.
2. Add identity parsing and service create behavior.
3. Add LinkedIn contact lookup helper.
4. Update API docs/schema descriptions.
5. Update `add_stakeholder` and `upsert_stakeholder` tools.
6. Add focused tests.

## Intentional Simplification

`copilotgtm:` Name-only dedupe is same-deal soft matching only. The ceiling is obvious duplicate
names inside one deal; the upgrade path is a candidate-confirmation API that returns matching
contacts/stakeholders before placeholder creation.
