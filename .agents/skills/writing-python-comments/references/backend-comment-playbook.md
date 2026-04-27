# Backend Comment Playbook

This playbook gives concrete comment shapes for Python backend work, plus a worked example showing what newcomer-readable density looks like in practice.

> **Read this first.** The default in this codebase is **denser, plain-English commenting** — enough that a junior engineer or new hire can read a file top to bottom and follow what's happening. If your output is sparse, you're under-commenting.

## Comment Decision Ladder

For each non-trivial block, walk this ladder:

1. Is the file missing a module docstring? → Add one. Name what the file owns and who calls into it.
2. Is this a public class/method? → Add a docstring naming the caller context and any caller-owned preconditions.
3. Is this one of the **mandatory comment sites** in SKILL.md (session boundary, dual write, cursor advance, retry boundary, provider quirk, ordering invariant)? → Add an inline comment. One sentence of *what*, one sentence of *why*.
4. Is the function long enough to need section headers? → Add `# --- Step N: ... ---` banners every 3+ phases.
5. Would a new engineer reading this need to ask someone what's going on? → Add a comment.
6. None of the above and the code is genuinely obvious? → Add nothing.

## Density Reference: A Worked Example

Below is what a reasonably-sized service file looks like at expected density. Notice the module docstring, public docstrings, section headers, and intent comments — together they let a newcomer follow the file without prior context.

```python
"""Calendar sync orchestration for provider-backed integrations.

Called by the integrations worker (scheduled sync, webhook fanout) and by the
reconnect bootstrap. This module owns the read-side of provider deltas; the
writer module (calendar/writer.py) owns persistence.

Two invariants this file protects:
  1. The org DB session is released before any provider call. Graph p95 is
     ~800ms and holding a pooled connection that long would pressure the org's
     connection pool under concurrent syncs.
  2. The cursor (delta link) is advanced only after the writer commits. A crash
     between fetch and commit must replay cleanly on the next run.
"""

# Provider page size. Graph caps at 1000 but smaller pages reduce per-call
# latency and keep memory bounded for orgs with very busy calendars.
GRAPH_PAGE_SIZE = 200


class CalendarSyncService:
    """Pulls provider deltas, normalizes them, and hands them to the writer.

    Caller owns lock acquisition (we do not serialize concurrent syncs for the
    same integration here — the worker queue does that via per-integration
    routing keys). Caller also owns the org boundary; this service trusts that
    organisation_id passed in has already been authorized upstream.
    """

    def __init__(self, organisation_id: UUID, integration_id: UUID) -> None:
        self.organisation_id = organisation_id
        self.integration_id = integration_id

    async def sync(self) -> SyncResult:
        """Run one sync pass: fetch, normalize, persist, then advance cursor.

        Returns a SyncResult summarizing created/updated/skipped counts and
        whether the integration needs a subscription renewal. A transient Graph
        failure raises; the worker is responsible for retrying with backoff.
        """
        # --- Step 1: load the cursor under a short-lived session ---
        # Cursor read is cheap (single PK lookup) so we can hold the session
        # for it. We release before the Graph call below.
        async for session in acquire_org_async_session(self.organisation_id):
            integration = await self._load_integration(session)
            cursor = integration.cursor

        # --- Step 2: fetch the next page of changes from the provider ---
        # Session is released here. Graph p95 ~800ms; holding a pooled
        # connection across this call would pressure the org's pool under
        # concurrent syncs from other workers.
        provider_payload = await provider.fetch_changes(
            integration.provider_id, cursor, page_size=GRAPH_PAGE_SIZE,
        )

        # --- Step 3: normalize attendees and resolve them to local users ---
        # Graph returns the organizer twice on recurring events (once on the
        # series, once on each exception). Dedup by email so attendance counts
        # don't double-up.
        normalized = self._normalize(provider_payload)

        # --- Step 4: persist and advance cursor ---
        # Order matters: writer.commit() must succeed before we advance the
        # cursor. If we reverse this and crash between, the next run skips the
        # uncommitted batch and we lose those events silently.
        async for session in acquire_org_async_session(self.organisation_id):
            await self._writer.commit(session, normalized)
            await self._advance_cursor(session, provider_payload.next_link)

        return SyncResult.from_payload(provider_payload, normalized)

    async def _advance_cursor(self, session, next_link: str) -> None:
        # Dual-write the canonical and legacy cursor keys during rollout.
        # Older queued workers (pre-March 2026 deploy) still read the legacy
        # `outlook_delta_link` key; remove this once the legacy worker pool is
        # fully drained — track in INTG-1843.
        metadata = self._integration_metadata(session)
        metadata[CANONICAL_CURSOR_KEY] = next_link
        metadata[LEGACY_CURSOR_KEY] = next_link
```

Notice what's happening here:

- The module docstring tells the reader what this file owns, who calls it, and the two invariants. A newcomer can stop reading after the docstring and still know when to use this module.
- Each public method has a docstring naming caller context and the contract.
- Section header banners (`# --- Step N: ... ---`) break the long function into phases.
- Every mandatory comment site (session release, ordering invariant, dual-write) has an intent comment in plain English.
- Comments name *numbers* (Graph p95 ~800ms, GRAPH_PAGE_SIZE=200) and *reasons* (rollout window, INTG-1843), not just abstract principles.

## API Route

Route comments explain auth, ownership, and side effects that are easy to miss.

```python
# The route accepts provider IDs from the client, but ownership is enforced
# here so a copied external ID cannot expose another org's integration state.
# The org filter is the trust boundary, not the provider ID.
integration = await get_integration_for_org(session, organisation_id, provider_id)
```

Avoid:

```python
# Get the integration
integration = await get_integration_for_org(...)
```

## Service

Service docstrings name caller-owned responsibilities and side effects in plain English.

```python
async def sync_calendar_changes(...):
    """Apply provider changes for one org-scoped integration.

    Called by connect bootstrap (immediately after OAuth grant), webhook
    fanout (per-event push), and the scheduled backfill task (every 15 min).
    The caller owns lock acquisition; this service only advances the cursor
    after normalized events and attendee state are committed.
    """
```

## Worker Task

Worker comments explain retry safety, ordering, and queue ownership at the top of the task.

```python
# This task may receive the same provider event more than once: Graph delivers
# at-least-once and Celery may retry on transient errors. The committed cursor
# is the idempotency boundary — retries replay reads but do not duplicate
# writes, because the writer upserts by provider event id.
await sync_service.apply_changes(...)
```

Avoid comments that narrate Celery mechanics:

```python
# Start the task
asyncio.run(run_sync())
```

## Migration

Migration comments name canonical and legacy behavior and the rollout window.

```python
# Backfill the canonical cursor key from the legacy Outlook-specific key. New
# code reads the canonical key first but keeps writing the legacy key during
# rollout so older queued workers (pre-March 2026 deploy) can still resume.
# Drop the legacy key in a follow-up migration after the worker pool is drained.
op.execute(...)
```

Avoid vague history:

```python
# Update old data
op.execute(...)
```

## Agent / Tool

Agent and tool comments state the immutable security boundary up front.

```python
# Tools receive the org boundary from AgentContext only; prompts cannot widen
# scope by passing a different organisation_id in tool arguments. This is
# enforced at the AgentContext level — see src/agents/context.py.
context = AgentContext(organisation_id=organisation_id, deal_id=deal_id)
```

## Provider Sync

Provider-sync comments explain cursor, subscription, webhook, and stale-data contracts in plain English.

```python
# Renewal repairs the webhook lease only. Advancing the delta cursor here
# would skip changes the next sync still needs to read — keep these two
# operations strictly separate.
await provider.renew_subscription(subscription_id)
```

```python
# Keep the legacy key during rollout so older queued workers (pre-March 2026)
# can still read cursor state until the canonical metadata contract is fully
# deployed.
metadata[LEGACY_CURSOR_KEY] = cursor
```

```python
# Accept a stale subscription for one renewal cycle. Graph occasionally
# delivers a change notification after our local expiration timestamp but
# before the renewal call wins; treating the record as live for that window
# avoids dropping legitimate notifications.
if subscription.is_recently_expired(now):
    ...
```

## DB Session Release

Comments here are mandatory whenever a session lifetime is shaped around slow external work.

```python
# Release the org session before the Graph call. Graph p95 is ~800ms and
# holding a pooled connection that long would pressure the org's pool under
# concurrent syncs from other workers.
provider_payload = await provider.fetch_changes(...)
```

```python
# Extract IDs and metadata into local vars before the LLM call so the org
# connection returns to the pool while the model response is pending. LLM
# responses can take 5-30s and we never want a connection held that long.
meeting_id = meeting.id
deal_id = meeting.deal_id
```

## Tests

Test comments explain the regression contract — what would break if this test ever passes silently after a code change.

```python
# Both legacy and canonical metadata keys must be written on every cursor
# advance. If a future change drops the legacy key prematurely, in-flight
# workers from the previous deploy can no longer resume and silently fall
# behind on calendar sync.
assert metadata[CANONICAL_CURSOR_KEY] == cursor
assert metadata[LEGACY_CURSOR_KEY] == cursor
```

Avoid:

```python
# Assert cursor equals cursor
assert metadata[CANONICAL_CURSOR_KEY] == cursor
```

## Bad Comments To Reject

```python
# Loop through events
for event in events:
```

```python
"""Helper for calendar logic."""
```

```python
# Get the integration
integration = await get_integration(...)
```

```python
"""This function updates calendar events."""
```

```python
# Core logic used everywhere
```

## Review Checklist

- The file has a module docstring naming what it owns and who calls it.
- Every public class and method has a docstring naming caller context and contract.
- Every mandatory comment site (see SKILL.md) has an intent comment.
- Long functions are broken up with `# --- Step N: ... ---` section headers.
- Comments name numbers (latencies, page sizes, thresholds) and references (issue IDs, docs URLs) where they have them.
- Comments are written in plain English a new engineer could follow without prior context.
- Usage claims are backed by code search, tests, routes, workers, migrations, or docs.
- The scanner output is clean **for both** low-value comments **and** missing-comment findings.
