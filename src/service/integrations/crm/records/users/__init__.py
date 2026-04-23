"""HubSpot CRM users (owners) record sync (phase 3).

Matched-only persistence: a `crm_users` row is written only when the HubSpot
owner's email matches an existing `public.users` row in the same organisation,
so `crm_users.user_id` always equals that app user's `user_id`.
"""
