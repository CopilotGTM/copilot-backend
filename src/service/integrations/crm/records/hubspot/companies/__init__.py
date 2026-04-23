"""HubSpot companies (accounts) record sync.

Phase 4 of the post-connect chain: schema → pipelines → users → **companies** →
contacts → deals → … → finalize. Populates `crm_companies` from HubSpot's
CRM Companies API and backfills `crm_deals.company_id` for rows that were
written while their company_external_id hadn't yet been resolved.
"""
