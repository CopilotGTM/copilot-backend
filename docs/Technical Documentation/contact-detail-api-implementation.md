# Contact Detail API Implementation

## Goal

Enhance `GET /users/crm/contacts/{contact_id}` so the contact detail page can render a deal-style overview:

- Main content shows contact research.
- Right panel shows contact details and deal involvement.
- Activity remains a later follow-up endpoint.

## Response Shape

The endpoint should return a flat contact detail payload with nested objects only where they represent real related entities.

```json
{
  "contact_id": "b798de7d-14c9-47ea-a616-547dba5493af",
  "first_name": "Vickesh",
  "last_name": "Shah",
  "full_name": "Vickesh Shah",
  "job_title": "Head of Sales - EMEA & APAC",
  "email": "vickesh@lemonedge.com",
  "additional_emails": [],
  "phone": null,
  "mobile_phone": null,
  "linkedin_url": null,
  "website": null,
  "source": "hubspot",
  "lifecycle_stage": null,
  "lead_status": null,
  "marketing_status": null,
  "company": {
    "id": "8fd8d9f7-3a8d-4d85-9c71-2f8e47a0a111",
    "name": "LemonEdge",
    "domain": "lemonedge.com",
    "website": "https://lemonedge.com",
    "industry": "Financial Services",
    "linkedin_url": null,
    "country": null
  },
  "owner": {
    "id": "72fbc4fd-7d0f-48d1-82c3-5dcbff8f1222",
    "name": "Nikhil Singhal",
    "email": "nikhil@example.com"
  },
  "address": null,
  "city": null,
  "state": null,
  "postal_code": null,
  "country": null,
  "timezone": null,
  "last_contacted_at": null,
  "last_activity_at": "2026-06-13T09:20:11Z",
  "created_at": "2026-06-13T09:20:11Z",
  "updated_at": "2026-06-20T12:44:08Z",
  "source_created_at": "2026-06-13T09:20:11Z",
  "source_modified_at": "2026-06-20T12:44:08Z",
  "synced_at": "2026-06-20T12:45:01Z",
  "research_summary": {
    "summary": "Vickesh Shah leads sales across EMEA and APAC at LemonEdge...",
    "last_researched_at": "2026-06-20T12:45:01Z"
  },
  "profile_enrichment": {
    "is_enriched": false,
    "source": null,
    "enriched_at": null,
    "data": {}
  },
  "deal_count": 1,
  "deal_involvement": [
    {
      "deal_id": "4e8e9c9e-1f2f-48d7-85f7-82ef1bd98911",
      "deal_name": "Vickesh-LemonEdge",
      "deal_status": "open",
      "stage_name": "Appointment Scheduled",
      "last_activity_at": null,
      "participant_id": "c4dc2fd1-cb5d-4a30-8138-18670d98f5f8",
      "relationship_role": null
    }
  ]
}
```

## Data Mapping

- Contact fields come from `crm_contacts`.
- `company` comes from explicit `CRMContact.company_id` first. If missing, fall back to domain matching using the contact email domain, matching current contact list behavior.
- `owner` comes from `CRMContact.owner_id`, `owner_name`, and `owner_email`.
- `research_summary.summary` comes from `CRMContact.contact_research`.
- `research_summary.last_researched_at` comes from `CRMContact.last_researched_at`.
- `profile_enrichment` wraps existing `profile_enrichment`, `profile_enrichment_source`, and `profile_enriched_at`.
- `deal_involvement` comes from approved, non-deleted `crm_deal_participants` rows joined to `crm_deals`.
- Keep `deal_involvement` intentionally small for the right panel: one join is enough to show deal name, stage, status, relationship role, and last activity. Deeper stakeholder fields belong on the deal page.

## Frontend Display Order

Main content:

1. Research Summary
2. Activity section later, from a separate endpoint

Right panel:

1. Details
2. Deal Involvement

## Implementation Notes

- Reuse the existing `GET /users/crm/contacts/{contact_id}` endpoint.
- Keep the API simple: avoid a nested `details` grouping and let the frontend decide field grouping.
- Do not add `stakeholder_summary`; there is no final product definition for it.
- Preserve organization isolation by filtering every contact, company, deal, and participant query by `organisation_id`.
- Return `404` when the contact does not belong to the current organization or is deleted.

## Tests

Add or update tests to cover:

- Contact detail returns nested `company` and `owner`.
- Contact detail includes `research_summary` from `contact_research`.
- Contact detail includes profile enrichment metadata.
- Deal involvement uses `crm_deal_participants.participant_id` and only exposes the compact right-panel fields.
- Deleted or cross-organization contacts are not returned.
