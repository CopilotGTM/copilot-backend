"""CRM record sync package.

Per-entity record syncs live under subpackages (pipelines, deals, etc.).
Shared infrastructure (chain builder, backfill contract, shared DTOs) lives
under `common/`.

See `docs/superpowers/specs/2026-04-22-hubspot-crm-pipelines-record-sync-design.md`.
"""
