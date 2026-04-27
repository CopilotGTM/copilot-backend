# Evaluation Cases

Use these cases to pressure-test the skill from **both** directions:
- **Over-commenting** — does the skill reject narration of obvious code?
- **Under-commenting** — does the skill produce enough plain-English context for a newcomer?

The original skill failed almost exclusively on under-commenting. Most of these cases exist to exercise that failure mode.

## Under-Commenting Cases (the common failure)

### Case U1: Service file with no module docstring

Scenario: A 500-line service module owns calendar sync orchestration and is called by three different workers and a bootstrap path.

Expected behavior: Add a module docstring naming what the file owns, who calls in, and any cross-file invariants. Without it, a newcomer cannot orient themselves.

Reject: Skipping the module docstring because "the class names are descriptive."

### Case U2: Public method without docstring

Scenario: `async def sync(self) -> SyncResult:` is the entry point of a service class. Type-annotated, but the caller context (which workers call it, when, with what locking expectations) is invisible from the signature.

Expected behavior: Add a docstring naming caller context, what the method does in plain English, what it returns, and any caller-owned preconditions.

Reject: Skipping the docstring because "the name `sync` is self-explanatory."

### Case U3: Session boundary before provider call

Scenario: A service acquires `acquire_org_async_session(org_id)`, reads the cursor, then releases the session and calls `await provider.fetch_changes(...)`.

Expected behavior: Add an intent comment explaining *why* the session is released before the Graph call — name the latency (e.g., "Graph p95 ~800ms") and the pool-pressure consequence.

Reject: No comment, on the grounds that "session release patterns are repeated elsewhere in the codebase."

### Case U4: Long function with no section headers

Scenario: A 60-line function does fetch → normalize → resolve attendees → persist → advance cursor.

Expected behavior: Add `# --- Step N: ... ---` banners above each phase so a newcomer can navigate the function visually.

Reject: Treating section headers as "noise" and leaving the function as one undifferentiated block.

### Case U5: Provider quirk in a conditional

Scenario: `if event.organizer.email in attendee_emails: attendee_emails.remove(event.organizer.email)` — this exists because Graph returns the organizer twice on recurring events.

Expected behavior: Add an inline comment naming the Graph quirk explicitly. A newcomer reading this line cannot guess why the dedup exists.

Reject: No comment, on the grounds that "the code is self-evident."

### Case U6: Order-dependent operations

Scenario: A function calls `writer.commit(events)` and then `advance_cursor(next_link)`. Reversing the order causes silent data loss on crash.

Expected behavior: Add a comment naming the ordering invariant and what would break if reversed.

Reject: No comment because "the order is obvious from the code."

### Case U7: Magic constant

Scenario: `GRAPH_PAGE_SIZE = 200` at module top.

Expected behavior: Add a comment naming what the number means, why this value was chosen, and the upper bound the provider allows.

Reject: Bare constant with no rationale.

## Over-Commenting Cases (the original failure mode)

### Case O1: Obvious private helper

Scenario: `_normalize_email(value: str) -> str | None` strips and lowercases a string.

Expected behavior: Choose no comment or no docstring. The name and types fully convey the contract.

Reject: `"""Helper for email normalization."""`

### Case O2: Mechanical narration

Scenario: A loop iterates over events and updates a counter.

Expected behavior: No comment.

Reject: `# Loop through events and increment the counter`

### Case O3: Stale comment cleanup

Scenario: A comment says a provider path is Outlook-only, but the code is now provider-agnostic.

Expected behavior: Delete the stale comment. Add a replacement only if a stable contract remains useful.

Reject: Adding a second comment that contradicts or dilutes the stale one.

## Backend-Specific Cases

### Case B1: Canonical/Legacy Metadata Dual Write

Scenario: A function writes both `calendar_delta_link` and `outlook_delta_link` during rollout.

Expected behavior: Compatibility comment explaining the migration contract: canonical key is the new contract, legacy key remains for older queued workers; name the rollout window or the issue ID where removal is tracked.

Reject: Comments that say only "set both keys" or "update metadata."

### Case B2: Org Isolation Query

Scenario: A query filters by `organisation_id` and an external provider ID supplied by the client.

Expected behavior: Comment that the org filter is the trust boundary — the external provider ID alone is not trusted, so a copied ID from another tenant cannot leak data.

Reject: Comments that merely say "filter by organisation_id."

### Case B3: Worker Retry / Idempotency

Scenario: A Celery task may replay provider notifications after transient failure.

Expected behavior: Comment at the top of the task naming the idempotency boundary, ordering requirement, and what makes retries safe (e.g., upsert-by-event-id at the writer).

Reject: Comments that narrate task start, argument parsing, or loops.

### Case B4: Public Docstring on Service Method

Scenario: A public service method is called by routes and workers and must not advance sync state until writes commit.

Expected behavior: Public docstring with caller context (which routes, which workers), what the method does in plain English, and the commit-before-advance invariant.

Reject: A docstring that says only "Sync calendar events" or enumerates parameters already typed in the signature.

### Case B5: Test Intent

Scenario: A regression test verifies that canonical and legacy metadata are both written on every cursor advance.

Expected behavior: Test comment explaining the rollout compatibility contract — what would break if a future change dropped the legacy key (in-flight workers from previous deploy can no longer resume).

Reject: Comments that paraphrase assertions line by line.

## Calibration

If you finish a 500-line service module with **fewer than 8 comments total** (module docstring + public docstrings + inline intent comments combined), you have almost certainly under-commented. Re-read the file looking for the under-commenting cases above.
