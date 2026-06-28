# Account Detail API Implementation

> Status: Implemented 2026-06-23

## Goal

Shape `GET /users/crm/accounts/{company_id}` so the account detail page can render a
deal-style overview without overloading the payload:

- Main content shows account research when available.
- Right panel shows account details, associated contacts, and associated deals.
- Activities are intentionally out of scope for this response and will come from a separate API.

Keep the contract simple, page-ready, and production-safe. Do not add speculative fields that are
not backed by current storage or a clear product requirement.

## Page Model

The account detail page is conceptually similar to deal detail, but the domain questions differ:

- Account details: "What company is this?"
- Contacts: "Who are the people at this account?"
- Deals: "What commercial opportunities exist under this account?"
- Account research: "What should the seller know about this account?"

Contacts and deals should therefore not reuse the same nested object shape as contact detail. They
answer different questions.

## Proposed Response Shape

```json
{
  "company_id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
  "name": "pssgway.com",
  "domain": "pssgway.com",
  "website": "https://pssgway.com",
  "industry": "Software",
  "lifecycle_stage": "opportunity",
  "owner_name": "Nikhil Singhal",
  "owner": {
    "id": "9a8b7c6d-e5f4-4321-9876-123456789abc",
    "name": "Nikhil Singhal",
    "email": "nikhil@example.com"
  },
  "description": "Passageway Tech Pvt LTD is evaluating CopilotGTM.",
  "company_type": "Private",
  "number_of_employees": 250,
  "annual_revenue": 50000000.0,
  "phone": "+1-555-0100",
  "linkedin_url": "https://linkedin.com/company/pssgway",
  "is_active": true,
  "address": "123 Tech Blvd",
  "city": "Pune",
  "state": "Maharashtra",
  "country": "IN",
  "postal_code": "411001",
  "timezone": "Asia/Kolkata",
  "contact_count": 5,
  "deal_count": 2,
  "pipeline_value": 75000.0,
  "account_summary": {
    "status": "not_available",
    "content": null,
    "last_researched_at": null
  },
  "contacts": [
    {
      "contact_id": "c1d2e3f4-a5b6-7890-cdef-123456789abc",
      "full_name": "Roop Singh",
      "email": "roop.s@pssgway.com",
      "job_title": "CEO"
    },
    {
      "contact_id": "d2e3f4a5-b6c7-8901-defa-23456789abcd",
      "full_name": "Abhishek D",
      "email": "abhishek.d@pssgway.com",
      "job_title": "Co-Founder and Director"
    }
  ],
  "deals": [
    {
      "deal_id": "8061bdd1-0036-4659-b834-6613c5a71b61",
      "name": "pssgway.com",
      "amount": 10000.0,
      "currency": "USD",
      "deal_status": "open",
      "stage_name": "Negotiation",
      "owner_name": "Nikhil Singhal"
    }
  ],
  "created_at": "2026-01-10T08:00:00Z",
  "updated_at": "2026-03-20T14:00:00Z"
}
```

## Field Decisions

### Contacts

Account detail contacts should be compact person summaries:

```json
{
  "contact_id": "c1d2e3f4-a5b6-7890-cdef-123456789abc",
  "full_name": "Roop Singh",
  "email": "roop.s@pssgway.com",
  "job_title": "CEO"
}
```

Do not include `deal_count` on account-detail contacts.

Reason: On an account page, the right panel contact section answers "who are the people at this
account?" The number of deals each person touches is contact-detail context and adds noise here.
If the user needs that deeper relationship context, they can open the contact detail page.

### Deals

Account detail deals should be compact opportunity summaries:

```json
{
  "deal_id": "8061bdd1-0036-4659-b834-6613c5a71b61",
  "name": "pssgway.com",
  "amount": 10000.0,
  "currency": "USD",
  "deal_status": "open",
  "stage_name": "Negotiation",
  "owner_name": "Nikhil Singhal"
}
```

This is intentionally different from contact detail's `deal_involvement` object. Contact detail
needs relationship fields such as `participant_id`, `relationship_role`, `stance`, and
`decision_power` because it answers "how is this person involved in the deal?" Account detail
answers "what opportunities exist under this account?"

Do not force one shared deal DTO for both contexts.

### Account Summary

Use a stable object so the frontend can build the main content area before account summary storage
is fully implemented:

```json
{
  "status": "not_available",
  "content": null,
  "last_researched_at": null
}
```

When durable account summary storage is added, map:

- `status`: `not_available`, `pending`, `in_progress`, `completed`, or `failed`
- `content`: markdown account summary
- `last_researched_at`: last completed research timestamp

Until then, return the stable object with null content rather than adding fake text or relying on
frontend-only placeholders.

### Activities

Do not include activities in account detail.

Activities will come from a separate API so account detail stays fast and predictable. The frontend
can reserve the section and load it independently later.

## Data Mapping

- Account identity and firmographics come from `CRMCompany`.
- `owner` comes from `CRMCompany.owner_id`, `owner_name`, and `owner_email`.
- `contact_count`, `deal_count`, and `pipeline_value` are computed by the account read service.
- `contacts` come from account-associated contacts using existing explicit-FK plus domain matching.
- `deals` come from active deals where `CRMDeal.company_id` matches the account.
- `pipeline_value` should remain open-deal-only, matching the account list behavior.

All account, contact, and deal queries must filter by `organisation_id` and exclude deleted rows.

## Backend Implementation Plan

1. Add `AccountDetailContactItem`.
   - Use it only for `AccountDetail.contacts`.
   - Keep `contact_id`, `full_name`, `email`, and `job_title`.
   - Keep `AccountContactItem.deal_count` for the standalone account contacts endpoint.

2. Add an account summary wrapper schema.
   - Suggested name: `AccountSummary`.
   - Fields: `status`, `content`, `last_researched_at`.
   - Default value for current implementation:

```python
AccountSummary(
    status="not_available",
    content=None,
    last_researched_at=None,
)
```

3. Update `AccountDetail`.
   - Include `account_summary: AccountSummary`.
   - Keep `contacts` and `deals` as separate arrays.
   - Use `AccountDetailContactItem` for compact account detail contacts.
   - Keep inherited account fields and `owner` from `AccountListItem`.

4. Update `AccountReadService.get_account_detail()`.
   - Continue verifying account ownership before loading children.
   - Return compact contacts without `deal_count`.
   - Return account commercial deal summaries as today.
   - Add stable `account_summary` object.

5. Update `accounts_handler.get_account_detail()`.
   - Stop passing contact `deal_count`.
   - Pass through `account_summary`.
   - Keep the endpoint response model as `GenericResponse[AccountDetail]`.

6. Update docs after implementation.
   - Update `docs/API Documentation/deals/crm-accounts-api.md`.
   - Keep this planning doc as the implementation reference.

## Tests

Add focused tests before implementation:

- `GET /users/crm/accounts/{company_id}` returns compact contacts without `deal_count`.
- `GET /users/crm/accounts/{company_id}` returns account deals with amount, currency, status,
  stage, and owner.
- `GET /users/crm/accounts/{company_id}` returns `account_summary.status = "not_available"` and
  null content when no research storage exists.
- Cross-organization or deleted accounts still return `404`.
- Account detail service keeps `pipeline_value` computed from open deals only.

## Non-Goals

- Do not add account activities to this endpoint.
- Do not add `source` to account detail.
- Do not add stakeholder/person relationship fields to account-detail deals.
- Do not add contact deal counts to account-detail contacts.
- Do not implement account research generation in this phase.
