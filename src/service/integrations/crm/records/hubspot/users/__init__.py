"""HubSpot CRM users (owners) record sync (phase 3).

Uses the match-or-mint identity bridge: matched emails reuse `public.users`
`user_id`; unmatched owners still land in `crm_users` with a minted UUID so
deal ownership can resolve on the first fact-table write.
"""
