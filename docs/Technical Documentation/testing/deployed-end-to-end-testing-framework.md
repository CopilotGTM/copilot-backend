# Deployed End-to-End Testing Framework

## Purpose

This document is the contract for end-to-end tests that run against a deployed
CopilotGTM backend. It is written for engineers and coding agents adding future
coverage.

The goal is not to accumulate one test for every function or branch. The goal is
to keep a small suite that proves important user outcomes across the real
boundaries where production failures occur:

```text
authenticated HTTP request
  -> record and tenant authorization
  -> PostgreSQL durable state
  -> queue publication
  -> worker execution
  -> Redis/SSE delivery when applicable
  -> durable terminal result
  -> provider state when applicable
```

A green route test with mocked sessions does not prove this flow. An HTTP 200 or
accepted Celery task ID is also not a successful end-to-end result.

## Authoritative locations

- `e2e/` contains deployed-environment tests and their suite-specific support.
- `e2e/conftest.py` owns shared configuration gates and runtime login.
- `scripts/e2e/run-dev.ps1` is the safe entry point for dev suites that obtain
  credentials from Azure Key Vault.
- `tests/` contains local unit, component, API, worker, migration, and contract
  tests. It is not the deployed E2E suite.

New deployed scenarios belong under `e2e/<domain>/`. Do not put live provider or
deployed-state tests under `tests/`.

## Test strategy

### Test outcomes, not implementation lines

Add a deployed E2E test when a change crosses at least two runtime boundaries or
when mocks cannot prove the behavior. Examples include authorization followed by
shared job reuse, API-to-Celery-to-database completion, SSE reconnect, and Gmail
initial-to-incremental cursor continuity.

Use a focused local test instead when the behavior is a pure transformation,
validator, query builder, or error mapping. One focused regression test at the
lowest useful boundary is enough; do not mirror every E2E assertion with a chain
of service and router mock tests.

### One scenario proves one business invariant

An E2E scenario may contain several steps when they are required to prove one
invariant. For example, “a second collaborator can reconnect to the active deal
summary” requires creating the job as one user and streaming it as another. Do
not split that into isolated request tests that lose the relationship.

Every scenario must state:

1. The business invariant.
2. The identities and records involved.
3. The action performed through a public boundary.
4. The durable or provider evidence that proves the outcome.
5. The forbidden outcome it guards against.
6. Its cleanup behavior.

## Execution tiers

| Tier | Target | Purpose | Normal trigger |
| --- | --- | --- | --- |
| Local focused | Current checkout | Pure logic and narrow regression checks | Before push |
| Deployed contract | Dev API | Authentication, validation, and read-only authorization checks | After dev deployment |
| Deployed pipeline | Dev API plus PostgreSQL, Redis, and workers | Durable asynchronous completion and SSE recovery | After dev deployment |
| Live provider | Dedicated dev integration and provider fixture | OAuth/provider semantics such as Gmail history and labels | Manual release gate |

Only the first tier runs directly against un-deployed local code. A deployed E2E
result is valid only after the exact commit or image under review has reached dev.
The test report must record that commit/image identity; otherwise it proves an
unknown deployment.

## Safety contract

Every state-changing E2E suite must satisfy all of these rules:

- It hard-fails for `prod`, `production`, or an environment name beginning with
  `prod-`.
- It requires an explicit suite-specific live opt-in such as
  `E2E_CRM_ENABLE_LIVE_MUTATION=true`.
- It uses dedicated E2E users, organisations, deals, meetings, integrations, and
  provider accounts. Never use customer records.
- Secrets come from Key Vault or process environment and are never written to a
  file, test output, assertion message, or committed fixture.
- Database verification uses a read-only credential. Mutations happen through
  public APIs or the same trusted external boundary a user invokes.
- Mutable fixtures contain a unique run identifier, for example
  `e2e-<suite>-<UTC timestamp>-<UUID>`.
- Cleanup runs in `finally`. Cleanup uses an authenticated API when available and
  only removes mutable records carrying the current run identifier.
- Append-only audit/job rows that have no public tag or delete contract are not
  rewritten for test convenience. Use dedicated fixtures, retain those rows, and
  report their exact IDs as test history.
- Failed cleanup is reported with exact record IDs; it must not invoke broad SQL
  deletion or hide the original test failure.
- Polling is bounded by a monotonic deadline and reports the last observed durable
  state on timeout.

Read-only negative tests may run without the live-mutation opt-in, but they must
still use dedicated test identities and block production.

## Shared fixture model

The CRM security suite needs four identities and stable records. Store credentials
and IDs in dev Key Vault; expose them to tests only as process environment values.

| Fixture | Meaning |
| --- | --- |
| `owner_a` | Deal collaborator who initiates work |
| `collaborator_b` | Different user in the same organisation with access to the same deal |
| `member_c` | Same-organisation member without access to the deal |
| `foreign_d` | User in a different organisation |
| `deal_primary` | Dedicated internal CRM deal shared by A and B |
| `deal_secondary` | Same-organisation deal used for substitution attempts |
| `event_primary` | Active meeting associated with `deal_primary` |
| `event_secondary` | Meeting not associated with `deal_primary` |
| `personal_integration_a` | User-scoped integration owned by A |
| `personal_integration_b` | User-scoped integration owned by B |

Fixtures must be validated at suite startup. The suite must stop before mutation
if an identity belongs to the wrong organisation, a deal collaboration is
missing, an event association is wrong, or a supposedly user-scoped integration
has different ownership.

The Gmail suite uses one dedicated Google Workspace mailbox connected only to the
dev E2E organisation. Its fixture messages use a run-specific subject prefix and
cover Primary, Updates, Promotions, Social, Forums, Draft, Sent, archived mail,
Spam, Trash, read/unread state, and an attachment.

## Reusable harness contract

New suites should reuse these behaviors rather than implementing them in each
test:

- `required_env(name)` for strict CI failure and convenient local skipping.
- Runtime login with an in-memory access token.
- A production-target guard plus explicit live-mutation opt-in.
- An authenticated HTTP client for each named actor.
- Read-only PostgreSQL probes scoped by `organisation_id` and exact record ID.
- A bounded `wait_for_*` helper that accepts a terminal predicate and reports the
  last durable state.
- An SSE reader that retains event IDs, reconnects with `Last-Event-ID`, and stops
  only on a terminal event or deadline.
- A run context containing suite name, run ID, target environment, deployed
  commit/image, start time, and created record IDs.

Before adding a new helper, search `e2e/conftest.py` and existing domain
`support.py` files. Promote a helper to shared support only when a second suite
actually needs the same behavior. Do not build a speculative framework hierarchy.

## Required durable evidence

Assertions must prove the last meaningful boundary, not the first accepted one.

| Flow | Insufficient evidence | Required evidence |
| --- | --- | --- |
| Async CRM job | HTTP 200/202 or Celery task ID | Correct tenant/deal/event/job identity and durable terminal state |
| SSE job | Stream opened | Authorized replay plus terminal frame matching the durable result |
| Authorization denial | Mocked dependency raised | Deployed public request denied and no durable job/mutation created |
| Gmail sync | Task dispatched or integration timestamp changed | Expected message identities, labels/state, cursor advance, and no excluded records |
| Provider send | Backend marked sent | Provider acceptance ID; delivery must not be claimed unless a delivery callback is persisted |

Tests may use logs for diagnosis, but logs alone are not completion evidence.

## Initial high-value scenario catalogue

Implement these as a small number of sequential business scenarios. They replace
large collections of narrowly mocked route tests.

### CRM-E2E-001: Shared deal summary authorization and completion

1. A generates a summary for `deal_primary`.
2. The API returns a job ID whose durable row matches A's organisation,
   `deal_primary`, and `deal_summary`.
3. B requests the same generation while it is active and receives the same job.
4. B connects or reconnects to the stream and receives a terminal result matching
   the durable job result.
5. C and D cannot generate or stream the job.
6. Denied attempts create no new job.

This proves record authorization, organisation-wide active-job reuse, queue/worker
completion, SSE visibility, and durable recovery in one flow.

### CRM-E2E-002: Meeting deal/event/job binding

1. A starts a supported job for `(deal_primary, event_primary)`.
2. Attempts using `event_secondary`, `deal_secondary`, or another job type are
   rejected.
3. B can stream the valid shared job only under the matching deal and event URL.
4. A request for `followup` returns the documented client error and creates no job.

This proves the complete tuple
`(organisation_id, deal_id, event_id, allowed_job_type)` at both creation and
delivery boundaries.

### CRM-E2E-003: Mutation policy boundaries

Use one parameterized scenario table rather than separate files for each route:

- link and unlink a deal messaging channel;
- create, update, research, and delete a stakeholder;
- refresh a user-scoped integration.

For each action, assert the permitted actor succeeds, the disallowed same-org
actor is denied, the foreign actor is denied, and the resulting durable state is
unchanged after denied requests. Integration refresh must also prove that A
cannot dispatch work for `personal_integration_b`.

### CRM-E2E-004: Tenant-local polymorphic participant reference

Create or select contact/company/user records in both E2E organisations. Adding a
lead participant succeeds only when `related_local_id` resolves to the declared
type in the lead's organisation. A wrong type, missing record, or foreign record
must create no participant.

### GMAIL-E2E-001: Initial mailbox policy

The current deployed Gmail lifecycle test is only a smoke subset of scenarios 001
and 002. None of the Gmail scenario IDs is complete until every requirement below
is implemented; scenario 003 has no deployed coverage yet.

Seed the dedicated mailbox with the run-specific fixture set, trigger a fresh
initial sync, and wait for durable completion. Assert:

- Primary, Updates, Draft, Sent, archived, read/unread, and attachment fixtures
  exist exactly once;
- Spam, Trash, Chat, Promotions, Social, and Forums fixtures do not exist;
- draft, sent, labels, attachment, and folder identity are correct;
- the stored cursor is non-empty; and
- a second no-change sync creates no duplicates.

### GMAIL-E2E-002: Incremental continuity

After `GMAIL-E2E-001`, create a draft, edit it, archive a Primary message, add an
Updates message, send a message, and move excluded fixtures between excluded
labels. Trigger incremental sync and assert every included change appears exactly
once, excluded content remains absent, folder/label state is current, and the
cursor advances.

The initial and incremental tests must use the same inclusion policy. If a mail
class is imported initially but cannot be maintained incrementally, the suite
must fail.

### GMAIL-E2E-003: Cursor recovery and bounded completeness

Exercise an expired history cursor and a mailbox result larger than one provider
page. The sync must recover without duplicates. If a configured safety cap is
reached while a next page exists, the job must expose an incomplete/failed state;
it must not report a complete initial sync.

## Test case template for future agents

Use this header in each new E2E test or scenario module:

```python
def test_<business_outcome>() -> None:
    """Prove <invariant> from <public input> through <durable/provider output>.

    Actors: <named fixture identities>
    Records: <named stable fixtures plus run-created records>
    Forbidden outcome: <what must never happen>
    Cleanup: <API cleanup or no mutation>
    """
```

Before writing the test, document its evidence chain in the module. If the final
assertion is only an HTTP status, mocked method call, log line, or enqueue ID, it
is not an asynchronous E2E test.

## Rules for coding agents and LLMs

When asked to add E2E coverage:

1. Trace the real API, database, queue, worker, SSE, and provider path first.
2. Search the existing `e2e/` support code and reuse the narrowest helper.
3. Add or extend one business scenario; do not create tests for every internal
   function touched by the implementation.
4. Never weaken production authorization through test-only endpoints or bypasses.
5. Never make a test pass by accepting multiple unrelated outcomes.
6. Assert exact tenant, record, job, and provider identities.
7. Treat timeouts as failures with last-state evidence, not automatic retries that
   hide instability.
8. Preserve unrelated staged and unstaged work; stage only the E2E slice when
   publication is requested.
9. Do not claim end-to-end success unless the exact deployed revision and final
   durable/provider outcome were observed.
10. Do not run live mutation suites against production.

## Implementation order

The framework should be expanded only as the first scenarios require it:

1. Add multi-user login and the shared production guard to `e2e/conftest.py`.
2. Add `e2e/crm_security/support.py` with CRM fixture validation, read-only job
   probes, bounded polling, and SSE replay support.
3. Implement `CRM-E2E-001` and `CRM-E2E-002`.
4. Extend `scripts/e2e/run-dev.ps1` with a `crm-security` suite selector and the
   required Key Vault mappings.
5. Add `e2e/gmail_sync/support.py` with dedicated-mailbox fixture discovery and
   read-only message/cursor probes.
6. Implement the three Gmail scenarios.
7. Extend the runner with `gmail-sync`; keep provider mutation behind a separate
   explicit opt-in.
8. Add a post-dev-deployment pipeline stage only after the suites are stable when
   run repeatedly against the dedicated fixtures.

Do not implement a generic framework package before steps 1-3 demonstrate a
second real reuse point.

## Release gate and report format

A change affecting one of these flows is ready to promote from dev only when its
relevant scenario passes against the deployed revision. Record:

```text
revision/image:
target environment:
suite and scenario IDs:
started/finished UTC:
actors and fixture IDs (never secrets):
created record IDs:
durable terminal states:
provider evidence IDs, when applicable:
cleanup result:
failures, skips, and known gaps:
```

A skipped live scenario is not a pass. A test executed against an unidentified or
older deployment is not release evidence.
