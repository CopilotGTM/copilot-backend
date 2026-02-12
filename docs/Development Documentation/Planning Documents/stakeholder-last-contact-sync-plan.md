# Stakeholder Last Contact Sync Plan

## Problem
- Stakeholders often show `Last Contact: Never` even when deals have recent matched emails or meetings.
- This reduces trust in stakeholder panels used by briefing and deal summary workflows.

## Goal
- Keep `deal_stakeholders.last_contact_at` current from deal activity.
- Update from:
1. Email associations (always completed communications).
2. Meeting associations only when the meeting is completed/past.

## Current-State Findings
- Email matching (`deal_email_matcher`) creates `DealEmailAssociation` but does not update stakeholder `last_contact_at`.
- Calendar matching (`deal_meeting_matcher`) creates `CalendarEventDealAssociation` but does not update stakeholder `last_contact_at`.
- Newly discovered stakeholders from email/calendar are created without `last_contact_at`.

## Options Considered
1. Update only during manual stakeholder create/update flows.
   - Rejected: misses most automated discovery and sync-driven activity.
2. Update only in timeline sync.
   - Rejected: indirect and delayed; timeline is derivative, not source-of-truth for contact recency.
3. Update directly in matching pipelines where associations are created.
   - Chosen: closest to source events, deterministic, minimal moving parts.

## Chosen Design
- Email matcher:
1. Resolve contact time from `email.received_at` (fallback `sent_at`).
2. On successful association insert, update matching non-deleted stakeholders for the same org/deal whose emails participated.
3. Set `last_contact_at = max(existing, contact_time)`.
4. Set discovered email stakeholders `last_contact_at` at creation.
- Meeting matcher:
1. Resolve contact time only if meeting is completed (`end_at <= now`; fallback to `start_at`).
2. On successful association insert, update matching non-deleted stakeholders for the same org/deal by attendee/organizer emails.
3. Set `last_contact_at = max(existing, contact_time)`.
4. Set discovered calendar stakeholders `last_contact_at` at creation only for completed meetings.

## Guardrails
- Preserve org/deal isolation in every stakeholder query.
- Never set `last_contact_at` from upcoming meetings.
- Only move timestamps forward (no regressions).
- Keep changes local to deal-worker matcher services (no API contract changes).

## Validation Plan
- Unit tests for:
1. Completed vs upcoming meeting contact-time resolution.
2. Email-driven stakeholder contact updates (only when newer).
3. Meeting-driven stakeholder contact updates (only when completed and newer).
- Run targeted checks:
1. `uv run ruff check --no-cache` on changed matcher/tool/test files.
2. `uv run pytest` for stakeholder tool tests and new matcher last-contact tests.

