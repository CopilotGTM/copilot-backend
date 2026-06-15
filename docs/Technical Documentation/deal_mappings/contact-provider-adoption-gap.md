# Contact Provider Adoption Gap

## Context

Calendar/Gmail discovery can create `crm_contacts` before the same person exists in HubSpot or Salesforce. Later, the CRM provider may sync a contact with the same email address and richer CRM data.

## Problem

Current HubSpot and Salesforce contact writers identify existing contacts by provider source mapping/external ID. They do not fall back to an organisation-level normalized email match when no provider mapping exists.

That means this flow can create duplicate contact rows:

1. Calendar/Gmail discovery creates `ada@acme.com` under the internal CRM integration.
2. Salesforce or HubSpot later creates/syncs the same person with provider external ID.
3. Provider sync does not find a source mapping for that external ID.
4. Provider sync inserts a new `crm_contacts` row instead of adopting the existing email-matched row.

Expected behavior is that the provider sync should be able to adopt the existing contact row, update it with CRM data, and attach the provider source mapping to the same `contact_id`. The exact implementation still needs to be decided after the production rollout.

## Changes Already Made

The participant-backed stakeholder cutover already includes these related changes:

- `crm_deal_participants` is now the active stakeholder surface.
- Provider deal participants are normalized into our canonical participant types: `contact`, `company`, and `user`.
- Provider-synced participant rows are marked with `participant_source = "CRM"`.
- `is_primary_contact` is maintained on `crm_deal_participants` for contact participants only.
- HubSpot and Salesforce participant writers now sync primary contact state both ways, including clearing stale `true` values to `false`.
- Company/user participants are not treated as primary contacts, even when the provider marks a company/account association as primary.

## Current Risk

Until contact adoption by email is implemented, CRM sync can still create a duplicate contact for a person already discovered from Calendar/Gmail. Participant linking will then point to whichever contact row the active sync path resolved, which can split history/research/profile data across duplicate contacts.
