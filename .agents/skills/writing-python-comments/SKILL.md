---
name: writing-python-comments
description: Use when writing, reviewing, or replacing Python comments and docstrings in backend code. Aim for newcomer-readable density — a junior engineer or a new hire should be able to read the file top to bottom and understand both what non-obvious blocks do and why. Triggers on services, workers, migrations, provider sync code, agents/tools, and regression tests.
---

# Writing Python Comments

Use this skill for Python/backend comments that **teach the reader** what the code is doing and why. Assume the reader is a newcomer to this codebase: a junior engineer, a new hire, or your future self six months from now after switching context. They should be able to read a file top to bottom and understand both what each non-obvious block does and why it does it that way.

> **The corrected rule.** "Explain the why, never the what" produces nearly empty files because LLMs read it as "stay silent." Replace it with: **Explain the why first, but include enough of the what for a newcomer to follow along without already knowing the codebase.**

## Required Workflow

1. Read the target file end to end before writing anything.
2. Skim callers and related files when the comment depends on cross-file context (a worker task, a route, a migration partner). If you can't find a clear caller in 1–2 searches, write the comment based on the file alone and say so honestly ("Called by scheduled renewal — see celery beat config").
3. For each function, class, or non-trivial block, decide:
   - **Module docstring** — what does this file own and who calls into it?
   - **Public docstring** — what does the caller need to know to use this safely?
   - **Inline intent comment** — why is this block written this way?
   - **Section header comment** — a short banner above a logical group of operations to orient the reader.
4. Write the comment. Default shape: **one sentence of what, one sentence of why**. Either can be omitted if truly obvious. A newcomer should not have to read the implementation to understand the intent.
5. After the file is fully commented, run `python .agents/skills/writing-python-comments/scripts/check_comment_smells.py` from the repo root. The scanner flags both bad comments **and** missing comments at high-risk sites.

## Default Coverage Expectations

These are baselines, not maximums. If your output is materially below these numbers, you're under-commenting — re-read the file and look for the trigger sites in the next section.

| File type | Expected coverage |
|---|---|
| Service module (200–700 LOC) | 1 module docstring; docstring on every public class and method; **5–12 inline intent comments** |
| Worker task | 1 module docstring; docstring on the task; 1 comment per retry / idempotency / ordering boundary |
| Migration | 1 module docstring naming canonical and legacy behavior; 1 comment per `op.execute` |
| Provider sync | 1 comment per cursor / subscription / webhook boundary; 1 comment per session-release before a provider call |
| Normalizer / data transform | Docstring on the public entry point; 1 comment per non-trivial mapping rule or provider quirk |
| Agent / tool | Docstring on every tool naming the immutable security boundary and required context |
| Test (regression) | 1 comment per test naming the contract being locked |
| Pure dataclass / enum / `__init__.py` | Module docstring only if non-obvious; otherwise nothing |

## Mandatory Comment Sites

Scan the file for these patterns. **Each one is a comment site by default** — if you write code at one of these sites without a comment, you have under-commented.

- `acquire_org_async_session(` or `acquire_master_async_session(` followed within ~20 lines by `await provider.*`, `await llm.*`, `await openai.*`, an HTTP call, or any other slow external call → comment why the session is released before the slow work.
- A query that filters by both `organisation_id` and an externally-supplied ID → comment that the org filter is the trust boundary, not the external ID.
- Dual writes or dual reads of a canonical key + legacy key → comment the migration contract and the rollout window.
- Webhook cursor / delta-link advancement → comment what cursor advancement means and what would break if it advanced too early.
- Celery task body → comment retry safety, ordering, and idempotency at the top of the task.
- `try/except` that swallows or transforms a provider error → comment what the swallow means for the caller (returns empty? retries later? marks subscription dead?).
- Conditional that depends on a provider quirk (timezone, all-day events, recurring exceptions, deleted vs cancelled, soft-delete tombstones, etc.) → comment the quirk; link a docs URL if you have one.
- Any place where the **order** of two operations matters for correctness → comment the ordering invariant.
- Any constant that encodes a business rule, threshold, SLA, or provider limit → comment what the number means and where it came from.

## Comment Reasons (checklist, not gate)

A comment is justified when **any** of these apply. You do not need to satisfy all of them.

- **intent** — why this code exists or why this approach was chosen.
- **invariant** — what must remain true for the code to be correct.
- **caller** — who calls this and in what situation.
- **boundary** — tenant, auth, transaction, queue, schema, or session boundary.
- **side effect** — writes, dispatches, cache mutations, external calls, emitted events.
- **migration contract** — canonical vs legacy behavior; rollout / rollback compatibility.
- **operational risk** — latency, retries, idempotency, pool pressure, ordering, rate limits, stale data.
- **provider quirk** — a non-obvious behavior of an external API that the code is working around.
- **newbie readability** — a block whose intent is clear to the author but not to a new engineer reading the file top to bottom.

The last reason is the safety net. If you'd hesitate to drop a new hire into this file and ask them to make a change, the file is under-commented.

## Newbie Readability Rule

Every comment must answer at least one of these in plain English:

1. **What is this block doing?** — "Resolve attendees from the provider payload, dropping organizers we've already linked to a user."
2. **Why is it written this way?** — "Graph returns the organizer twice on recurring events; dedup by email so attendance isn't double-counted."
3. **What would break if a future change ignored this?** — "The cursor must be written *after* the events commit; reversing this order causes silent data loss on crash."

A comment that only restates the function name fails this rule. A comment that gives no context for the *why* also fails. Target: a smart engineer new to this repo can read the file and understand what's happening without asking anyone.

## Component Templates

### Module Docstring

Required on every non-trivial module. Name what the module owns, who calls into it, and one or two important constraints.

```python
"""Calendar sync orchestration for provider-backed integrations.

Called by the integrations worker (scheduled sync, webhook fanout) and by the
reconnect bootstrap. This module owns the read-side of provider deltas; the
writer module owns persistence. The cursor is advanced here only after the
writer commits, so a crash mid-batch replays cleanly on the next run.
"""
```

### Public Docstring

Required on every public class and method whose contract isn't obvious from the name and types alone.

```python
async def sync_calendar_changes(
    organisation_id: UUID,
    integration_id: UUID,
) -> SyncResult:
    """Pull and persist new calendar events for one org-scoped integration.

    Called by the integrations worker on every scheduled tick and by the
    reconnect bootstrap immediately after a fresh OAuth grant. The org session
    is released before each Graph call so a slow provider response (Graph p95
    ~800ms) cannot pin a pooled connection for the full batch.

    Returns a SyncResult summarizing created/updated/skipped counts and whether
    the integration needs a subscription renewal.
    """
```

### Section Header Comment

Short banners orient the reader inside a longer function. Use them whenever a function has 3+ logical phases — they're cheap and dramatically improve readability.

```python
# --- Step 1: pull the next page of provider changes ---
...

# --- Step 2: normalize attendees and resolve them to local users ---
...

# --- Step 3: persist events, then advance the cursor ---
...
```

### Inline Intent Comment

Default shape: one line of *what*, one line of *why*. Either can be omitted if truly obvious.

```python
# Release the org session before the Graph call. Graph p95 is ~800ms and we
# don't want to hold a pooled connection for the full request.
async for session in acquire_org_async_session(organisation_id):
    cursor = session.scalar(...)
provider_payload = await provider.fetch_changes(cursor)
```

### Compatibility / Migration Comment

```python
# Write both keys during rollout. Older queued workers still read the legacy
# key; remove this dual-write once the legacy worker pool is fully drained.
metadata[CANONICAL_CURSOR_KEY] = cursor
metadata[LEGACY_CURSOR_KEY] = cursor
```

### Test Intent Comment

```python
# Replaying the same webhook must not advance the cursor. If this regresses,
# a transient Graph failure causes silent data loss on retry.
```

## Hard Rules

Keep these prohibitions:

- Don't restate a single line that the code already makes obvious (`# increment counter` above `i += 1`).
- Don't add `"""Helper for X"""` docstrings that say nothing.
- Don't claim "used everywhere" or "core logic" without evidence.
- Don't paraphrase assertions in tests line by line.
- Don't write hand-maintained file maps that will rot.

Add these positive requirements:

- **Do** comment every mandatory comment site listed above.
- **Do** add a module docstring to every non-trivial file.
- **Do** add public docstrings on classes and public methods unless the name and types fully convey the contract.
- **Do** include enough plain-English context that a new engineer can follow the file without external help.
- **Do** prefer two short comments to one terse cryptic comment if it helps the reader.

## Backend-Specific Rules

- **Org isolation** — name the boundary being protected in plain English ("only this org's integrations can be returned; the external provider ID alone is not trusted").
- **DB sessions** — explain *why* the session is released before slow work, in numbers when you have them ("Graph p95 ~800ms; holding a pooled connection that long pressures the org's pool").
- **Workers** — explain idempotency, retry behavior, ordering, and queue ownership at the top of the task body.
- **Migrations** — name canonical vs legacy behavior and the rollout window. Include downgrade limits if relevant.
- **Provider sync** — explain cursor / subscription / webhook contract and what stale data is tolerated.
- **Agents / tools** — state the immutable boundary (org, deal) and the required context up front in the docstring.

## References

- `references/backend-comment-playbook.md` — denser examples and a fully-commented worked example showing expected density.
- `references/evaluation-cases.md` — pressure cases for both over- and under-commenting.

## Scanner

`scripts/check_comment_smells.py` flags both:

1. **Low-value comments** — the "get/set/loop through/helper/used everywhere" patterns.
2. **Missing comments at high-risk sites** — public functions without docstrings, modules without docstrings, `acquire_*_async_session` calls without a nearby intent comment.

Treat both kinds of findings as required to address before declaring the file done.
