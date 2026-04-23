"""HubSpot contacts record sync.

Phase 5 of the post-connect chain: schema → pipelines → users → companies →
**contacts** → deals → … → finalize. Populates `crm_contacts` from HubSpot's
CRM Contacts API, resolves `contact.company_id` against the
already-synced-in-this-chain `crm_companies` snapshot, and backfills
`crm_deals.primary_contact_id` (+ name + email) for deals that landed before
their primary contact was visible.
"""
