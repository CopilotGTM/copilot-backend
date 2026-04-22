"""Shared infrastructure for CRM record syncs.

Modules:
  - record_sync_types: shared DTO base + mixins for per-phase result shapes.
  - backfill: CRMDeal FK-resolution contract (safety-net for phase-ordering edge cases).
  - hubspot_chain: Celery chain builder for the HubSpot post-connect initial sync.
"""
