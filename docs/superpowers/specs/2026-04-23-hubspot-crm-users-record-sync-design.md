# HubSpot CRM Users (Owners) Record Sync — Design

**Status:** 2026-04-23 — phase 3 of the HubSpot record-sync series. Builds on the shared infrastructure introduced in `2026-04-22-hubspot-crm-pipelines-record-sync-design.md` (Celery chain, backfill contract, task_signature helper, record_sync_types).

---

## ⚠️ REVISION 2026-04-23 — Identity-bridge design supersedes matched-only

**The original "matched-only" rule below is OBSOLETE.** As-shipped behaviour:

- **Every non-malformed HubSpot owner produces a `crm_users` row.** No owner is skipped for being absent from `public.users`.
- **`user_id` is the shared identifier across `public.users` and `crm_users`, but not enforced by FK.** Two cases:
  - Owner email matches a `public.users` row in the same org → `crm_users.user_id = public.users.user_id` (unchanged from original).
  - No match (or owner has no email) → `crm_users.user_id = uuid4()` minted by the writer. **No `public.users` row is written.**
- **Signup-flow convergence (future, not in this phase):** when a `public.users` row is later created for email E in org O (via signup or admin-create), the creator is expected to look up `crm_users WHERE email=E AND organisation_id=O` first. If one exists, use its `user_id` as `public.users.user_id` so the identifier converges in both tables. If none exists, mint fresh as normal.
- **Renamed / new counters.** `skipped_no_email` and `skipped_unmatched` are gone (we no longer skip). `UserWriteResult` and `UserSyncResult` now carry `users_matched_public` (user_id reused from `public.users`) and `users_minted_uuid` (fresh UUID).
- **`OwnerSkipReason` enum deleted.** `OwnerMatchResult` is now just `(owner, matched_app_user_id: UUID | None)`; the writer's `_resolve_user_id` returns either the matched PK or a minted uuid4.

### Why the change

Matched-only meant a HubSpot owner who hadn't (yet) signed up as an app user produced no `crm_users` row, which in turn meant deals owned by them landed with `owner_id=NULL`. That defeats the entire purpose of the record-sync pipeline's write-time FK resolution. Minting a UUID in `crm_users` keeps deal FKs resolvable while still giving the eventual signup flow a handle to converge identities on.

Sections 1, 2.matched-only paragraph, 3.skip-rules, 4.UNMATCHED classification, 7.UserSyncResult counter list, 9.edge cases are all superseded by this block. The rest of the file (fetcher, chain, orchestration, deactivation, deal backfill) remains accurate.

---

## 1. Goal

Ingest HubSpot **owners** (which are the CRM-side view of users who can own records) into CopilotGTM's internal `crm_users` table. Stable identity for each human in the org is guaranteed by anchoring `crm_users.user_id` to `public.users.user_id` whenever an app user already exists for that email.

**Key product rule (unique to this phase):** a `crm_users` row is written **only when the HubSpot owner's email matches an existing `public.users` row in the same organisation**. Unmatched owners are intentionally skipped. Their data becomes syncable later — when the person signs up as an app user, a subsequent users-sync run will pick them up.

Sync is strictly HubSpot → CopilotGTM. No write-back. Runs as link 3 of the post-connect chain; also re-dispatchable as a standalone Tier 3 task.

## 2. Strategic Context (non-normative)

One CRM per organisation going forward — so we don't have to reconcile HubSpot owner identity against Salesforce user identity. This lets us treat `crm_users.user_id` as a single-source-of-truth identifier for a human inside an org's CRM universe.

Anchoring `crm_users.user_id` to `public.users.user_id` on email match means:

- Analytics, "who owns this deal?", notification recipients, and AI-agent ownership queries all resolve to one identifier.
- When a future phase needs to check "is this CRM owner also an app user?", it's a PK equality comparison, not a separate join-by-email.

The *matched-only* rule avoids a different problem — creating ghost `crm_users` rows (synthetic UUIDs) for every HubSpot owner in the portal, including people who will never log into CopilotGTM. Those rows would bloat the table, complicate reporting ("which of these are real users?"), and carry no downstream value. We only persist a CRM user when they correspond to a real org member.

## 3. Scope

### In scope

- `GET /crm/v3/owners?archived=false` with pagination.
- Deterministic email match against `public.users` scoped by `organisation_id` (case-insensitive, whitespace-trimmed).
- Active-snapshot persistence into `crm_users`: create (matched only), update, reactivate, deactivate. Mirrors the phase-2 pattern.
- **Full HubSpot owner field capture** — every field the owners endpoint returns is stored (`external_id`, `email`, `first_name`, `last_name`, `full_name`, `user_role`, `primary_team_id`, `team_ids`, `source_created_at`, `source_modified_at`, `data_payload`, and `properties` containing `hubspot_user_id`, `hubspot_user_id_including_inactive`, `teams[]`).
- Backfill at end of sync: update `CRMDeal.owner_id`, `owner_name`, `owner_email`, `created_by_user_id`, `updated_by_user_id` for rows whose `*_external_id` matches a just-synced matched owner. Uses the shared `backfill_crm_deals` contract.
- Tier 3 Celery task `integrations.sync_hubspot_users`; appended to `build_hubspot_initial_sync_chain` between pipelines and finalize.
- Sync history via `SyncHistoryManager`; structured metadata under `connection_metadata.record_sync.users`.

### Out of scope

- App-user signup side (when someone signs up AFTER users-sync has already run, automatically create the matching `crm_users` row). That is a signup-flow change tracked separately.
- Creating `crm_users` rows for unmatched HubSpot owners. **Deliberate product choice.**
- Backfilling `CRMDeal.owner_name` / `owner_email` for **unmatched** HubSpot owners. If the deal row has an `owner_external_id` that doesn't correspond to any app user, display fields stay as whatever phase 6 deal sync populated (typically NULL). Future work: phase 6 deal sync can denormalize name/email directly from the HubSpot deal fetch instead of going through `crm_users`.
- Contacts, companies, deals, stage history, associations — separate phases.
- Salesforce users — separate integration, separate future phase. (Project direction: single-CRM-per-org, so in practice a given org syncs either HubSpot or Salesforce, never both.)
- Webhook ingestion of owner changes.
- Tests. Single end-phase task per project convention.

## 4. Product Rules

1. A HubSpot owner whose email matches a `public.users` row in the same organisation (case-insensitive, whitespace-trimmed) produces a `crm_users` row with `user_id = public.users.user_id`. Same UUID on both sides.
2. A HubSpot owner whose email does **not** match any `public.users` row in the same org is **skipped** — no `crm_users` row is created. Counted in `skipped_unmatched`.
3. A HubSpot owner with no email is **skipped** — counted in `skipped_no_email`. No match is possible.
4. Match evaluation happens at **row creation time only**. Once `crm_users.user_id` is assigned, it is immutable (it's the PK and is FK-referenced by `CRMDeal.owner_id` etc.).
5. Owners present in a prior sync but absent from the current HubSpot response have their `crm_users.is_active` set to `false`. No hard or soft delete.
6. HubSpot owner `id` goes to `crm_users.external_id`. HubSpot `userId` (which is a different identifier — the HubSpot user id, not the owner id) is preserved in `crm_users.properties["hubspot_user_id"]` for forensic / future cross-reference, but is not authoritative for deal ownership.

## 5. High-Level Pipeline

```
[Entry points]                          [Orchestrator]                    [Units]

post-connect chain link 3  ─┐                                             ┌─ 1. Fetcher
                            │                                             │    /crm/v3/owners (paginated)
future manual re-run       ─┼─►  Tier 3 ─► service.run(                   ├─ 2. App-user email index
                            │    Celery     org_id, integration_id )      │    SELECT email, user_id
future post-signup hook    ─┘    task                                     │      FROM public.users
                                                                          │      WHERE organisation_id = :org
                                                                          │
                                                                          ├─ 3. Normalizer
                                                                          │    raw HubSpot owner → NormalizedHubSpotOwner
                                                                          │
                                                                          ├─ 4. Writer
                                                                          │    MATCHED owners → upsert crm_users
                                                                          │    UNMATCHED → drop with count
                                                                          │    active-snapshot deactivation
                                                                          │
                                                                          └─ 5. Deal backfill
                                                                               backfill_crm_deals with owner FK op(s)
```

Session lifecycle (same shape as pipelines):

1. **Service Session A.** Load `KnowledgeIntegration`, build `IntegrationTokenInfo`. **Same session** also loads the `public.users` email index (`{normalized_email → user_id}`) for this org — reading both while the session is open.
2. **No session held.** Paginated HubSpot owners fetch.
3. **In-memory normalization + match resolution.** Pure Python.
4. **Service Session B.** Writer upserts matched rows + deactivates missing + runs `backfill_crm_deals`, then commits.
5. Return `UserSyncResult` to the Tier 3 task.

## 6. HubSpot API Contract

### Endpoint

```
GET https://api.hubapi.com/crm/v3/owners?archived=false&limit=100
```

Scope required: `crm.objects.owners.read` (already held — see `HubSpotProvider._get_scopes`).

### Response shape

```json
{
  "results": [
    {
      "id": "12345",
      "email": "alice@acme.com",
      "type": "PERSON",
      "firstName": "Alice",
      "lastName": "Smith",
      "userId": 67890,
      "userIdIncludingInactive": 67890,
      "teams": [
        { "id": "111", "name": "Sales", "primary": true },
        { "id": "222", "name": "AMER",  "primary": false }
      ],
      "createdAt": "2024-01-15T08:00:00Z",
      "updatedAt": "2024-06-02T14:22:31Z",
      "archived": false
    }
  ],
  "paging": { "next": { "after": "cursor_opaque_token" } }
}
```

### Pagination

Standard v3 cursor: `paging.next.after` → next request adds `&after=<token>`. Fetcher iterates until `paging.next` is absent. Default `limit=100` (HubSpot caps at 500; 100 keeps each page's latency low).

### Retry / auth

Same as phase 2: 401-after-refresh surfaces as `provider_auth`; 429 and 5xx retry via the Tier 3 task's Celery retry policy; malformed payload (non-list `results`) raises.

## 7. Normalization

Pure module: `src/service/integrations/crm/records/users/user_normalizer.py`.

### Types

```python
@dataclass(slots=True, frozen=True)
class NormalizedHubSpotOwner:
    external_id: str                      # HubSpot owner id
    email: str | None                     # lowercased, stripped; None if missing/blank
    first_name: str | None
    last_name: str | None
    full_name: str | None                 # "first last" trimmed, fallback to email
    user_role: str | None                 # HubSpot `type`, e.g. "PERSON"
    team_ids: tuple[str, ...]             # ids of every team this owner belongs to
    primary_team_id: str | None           # id of the team with primary=true (if any)
    source_created_at: datetime | None    # parsed from createdAt
    source_modified_at: datetime | None   # parsed from updatedAt
    properties: dict[str, Any]            # { hubspot_user_id, hubspot_user_id_including_inactive, teams }
    data_payload: dict[str, Any]          # raw HubSpot owner unchanged, for replay
```

### Rules

- **Email.** `email.strip().lower()` if non-empty string, else `None`.
- **`full_name`.** `" ".join(p for p in [first, last] if p).strip()`, fallback to `email` if both names are empty, else `None`.
- **`team_ids`.** List of stringified `team.id` for every team entry. Deduplicated, order preserved.
- **`primary_team_id`.** The `id` of the first team where `team.primary is True`. If no primary flag, `None`.
- **Timestamps.** ISO-8601 parsed with `Z → +00:00`. Unparseable → `None`.
- **Skip rules at normalization time:**
  - Missing or non-string `id` → drop, `skipped_malformed += 1`.
  - `archived=true` somehow slipped through the `archived=false` filter → drop silently (defensive).
- **`properties` payload shape:**

  ```json
  {
    "hubspot_user_id": 67890,
    "hubspot_user_id_including_inactive": 67890,
    "hubspot_type": "PERSON",
    "teams": [ { "id": "111", "name": "Sales", "primary": true }, ... ]
  }
  ```

## 8. App-User Match Resolution

Match happens after normalization, in memory, inside the service:

```python
@dataclass(slots=True, frozen=True)
class OwnerMatchResult:
    owner: NormalizedHubSpotOwner
    matched_app_user_id: UUID | None    # None → skipped unmatched
```

Algorithm:

1. Build `email_index: dict[str, UUID]` — `{lower(stripped email) → user_id}` from `public.users WHERE organisation_id = :sync_org`. **Same-org scope is load-bearing** — cross-org matches would leak `user_id`s across tenants.
2. For each `NormalizedHubSpotOwner`:
   - If `owner.email is None`: classify as **skipped_no_email**, do not emit.
   - Else look up `email_index.get(owner.email)`.
   - If present: emit `OwnerMatchResult(owner, matched_app_user_id=<user_id>)`.
   - If absent: classify as **skipped_unmatched**, do not emit.

Only `OwnerMatchResult` entries where `matched_app_user_id is not None` reach the writer.

## 9. Persistence

### Writer contract

One class: `src/service/integrations/crm/records/users/user_writer.py`.

```python
class HubSpotUserWriter:
    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None: ...

    async def apply(
        self,
        *,
        matches: list[OwnerMatchResult],        # only emitted matched owners
        all_external_ids: set[str],             # every owner id HubSpot returned,
                                                # matched or not — drives the
                                                # deactivation scope
    ) -> UserWriteResult: ...
```

Mandatory filters on every statement: `organisation_id = :org AND integration_id = :int`.

### Write algorithm

**Pass A — upsert matched owners.**

Load every non-deleted `crm_users` row for `(organisation_id, integration_id)` keyed by `external_id`. For each `OwnerMatchResult`:

- **New key** (no existing row): insert with
  - `user_id = match.matched_app_user_id` — the app user's UUID
  - `external_id = owner.external_id`
  - `email`, `first_name`, `last_name`, `full_name`, `user_role`
  - `primary_team_id`, `team_ids`
  - `properties`, `data_payload`
  - `source_created_at`, `source_modified_at`
  - `is_active=True`, `synced_at=now`, `last_seen_at=now`
  - Bump `users_created`.
- **Existing, was active**: update every field except `user_id` (immutable). Bump `users_updated`.
- **Existing, was inactive**: same field updates plus `is_active=True`. Bump `users_reactivated`.

**Pass B — active-snapshot deactivation.**

Any existing `crm_users` row for `(organisation_id, integration_id)` that was previously `is_active=true` and whose `external_id` is **not** in the incoming `all_external_ids` set (regardless of match status) → `is_active=false`. Bump `users_deactivated`.

Note: `all_external_ids` is the full HubSpot owner set — matched + unmatched. A row that was previously matched (and thus persisted) but whose owner is now missing from HubSpot gets deactivated correctly.

Commit at end of the writer's work.

### Match edge case — duplicate app users for same email

`public.users.email` is globally unique, so no duplicates. The email index is a safe 1:1 map by construction.

### Match edge case — HubSpot owner matches an app user from a DIFFERENT org

Impossible by construction because the email index is filtered `WHERE organisation_id = :sync_org`. Even though `public.users.email` is globally unique, the lookup is scoped to the syncing org's app users only.

### Result shape

```python
@dataclass(slots=True)
class UserWriteResult:
    users_created: int = 0
    users_updated: int = 0
    users_reactivated: int = 0
    users_deactivated: int = 0
```

## 10. Deal Backfill at End of Users Sync

After the writer commits, the service calls `backfill_crm_deals` with a small set of FK ops in the same Session B:

```python
ops = [
    # owner
    DealFkBackfillOp(
        external_col="owner_external_id",
        fk_col="owner_id",
        name_col="owner_name",
        label_col=None,
        mapping={
            owner.external_id: (matched_app_user_id, owner.full_name, None)
            for owner, matched_app_user_id in emitted_matches
        },
    ),
    # created_by
    DealFkBackfillOp(
        external_col="created_by_user_external_id",
        fk_col="created_by_user_id",
        mapping={ ...same mapping keys, FK-only... },
    ),
    # updated_by
    DealFkBackfillOp(
        external_col="updated_by_user_external_id",
        fk_col="updated_by_user_id",
        mapping={ ...same mapping keys, FK-only... },
    ),
    # owner_email backfill uses the display-field slot of the owner op? No —
    # the owner op above covers owner_name only. owner_email gets its own op
    # because DealFkBackfillOp's shape is (FK + name [+ label]). We model
    # owner_email as a second display slot on a synthetic op? No: extend the
    # op shape instead → see "Backfill contract extension" below.
]
```

### Backfill contract extension

The current `DealFkBackfillOp` supports `(FK) | (FK + name) | (FK + name + label)`. The users sync wants to populate three fields on `CRMDeal`: `owner_id`, `owner_name`, `owner_email`. To fit the contract, we add one more column to the allowlist and one more display-field slot:

```python
@dataclass(slots=True, frozen=True)
class DealFkBackfillOp:
    external_col: str
    fk_col: str
    mapping: Mapping[str, tuple[UUID, str | None, str | None, str | None]]
        # external_id → (internal_id, name, label_or_email, extra_display?)
    name_col: str | None = None
    label_col: str | None = None
    email_col: str | None = None          # NEW — for owner_email and future
```

Shapes become:

| Shape | name_col | label_col | email_col | Mapping tuple |
|---|---|---|---|---|
| FK-only | None | None | None | `(id, None, None, None)` |
| FK + name | set | None | None | `(id, name, None, None)` |
| FK + name + label | set | set | None | `(id, name, label, None)` |
| FK + name + email | set | None | set | `(id, name, None, email)` |

Allowlist additions: `owner_email`, `primary_contact_email` to `_ALLOWED_DISPLAY_COLS`; shape validator extended to reject the new forbidden combination `email_col set AND name_col None` (same principle as label without name).

Keeping the backfill contract a single function across all phases is more valuable than one-off helpers per phase, so we extend rather than branch.

## 11. Folder Layout

### New files (service layer)

```
src/service/integrations/crm/records/users/
├── __init__.py
├── user_sync_service.py   # HubSpotUserRecordSyncService
├── user_normalizer.py     # normalize_owners + email-index helper
├── user_writer.py         # HubSpotUserWriter
└── user_types.py          # NormalizedHubSpotOwner, OwnerMatchResult,
                           # UserWriteResult, UserSyncResult
```

### New files (worker layer)

```
src/workers/integrations_worker/
├── providers/crm/hubspot/
│   └── owners_fetcher.py                   # HubSpotOwnersFetcher
└── tasks/crm/
    └── hubspot_user_record_sync_task.py    # Tier 3
```

### Modified files

- `src/service/integrations/crm/records/common/backfill.py` — extend `DealFkBackfillOp` per §10; add `owner_email` / `primary_contact_email` to display-col allowlist.
- `src/service/integrations/crm/records/common/hubspot_chain.py` — add `task_signature("integrations.sync_hubspot_users", …)` between pipelines and finalize.
- `src/service/integrations/initial_sync_service.py` — `apply_post_connect_state` stamps `record_sync.users.status="queued"`.
- `src/workers/integrations_worker/config.py` — add `TASK_LIMITS` + `task_routes` entry for `integrations.sync_hubspot_users` (on `integrations_priority`).
- `src/workers/integrations_worker/tasks/__init__.py` + `tasks/crm/__init__.py` — register the new task.

### Unchanged

- `crm_users` model — all needed columns exist (`external_id`, `email`, `first_name`, `last_name`, `full_name`, `user_role`, `primary_team_id`, `team_ids`, `is_active`, `source_created_at`, `source_modified_at`, `properties`, `data_payload`). **No migration needed.**
- `public.users` — read-only.

## 12. Sync Orchestration

### Tier 3 task

- Name: `integrations.sync_hubspot_users`
- Queue: `integrations_priority`
- Retries: 3, base delay 60s
- Soft / hard time limit: 300s / 360s (owners per portal are small, typically <500)

Lifecycle follows phase 2 pattern exactly:

1. UUID coercion.
2. `SyncHistoryManager.start_fresh(...)`.
3. `HubSpotUserRecordSyncService().run(...)`.
4. `SyncHistoryManager.complete_fresh(status=SUCCESS, metadata=result.to_metadata())`.
5. Update `connection_metadata.record_sync.users`. **Never** write `initial_sync_status`.
6. `SoftTimeLimitExceeded` → record failure + **re-raise** (chain-abort guarantee).
7. `UserSyncConfigurationError` → mark history `FAILED` + re-raise.
8. Provider / DB errors → mark history `FAILED` + re-raise for Celery retry.

### Chain position

```
schema → pipelines → users → finalize           ← this phase adds the users link
                  ↑ this phase appends here
```

`build_hubspot_initial_sync_chain` becomes:

```python
return chain(
    task_signature("integrations.sync_hubspot_deal_schema", kwargs=kwargs),
    task_signature("integrations.sync_hubspot_pipelines",  kwargs=kwargs),
    task_signature("integrations.sync_hubspot_users",      kwargs=kwargs),   # NEW
    # phase 4: companies …
    task_signature("integrations.mark_hubspot_initial_sync_succeeded", kwargs=kwargs),
).set(link_error=failure_terminator)
```

### Post-connect metadata

`apply_post_connect_state` for HubSpot extends the `record_sync` placeholder:

```json
{
  "record_sync": {
    "pipelines": { "status": "queued" },
    "users":     { "status": "queued" }
  }
}
```

### Manual re-run

Any future endpoint dispatches `integrations.sync_hubspot_users` directly for a single-phase re-run. The script `send_hubspot_deal_schema_sync.py` already supports `--pipelines-only`; a `--users-only` flag lands alongside this phase.

### Service result

```python
@dataclass(slots=True)
class UserSyncResult(RecordSyncResult):
    fetched_owners: int = 0
    users_created: int = 0
    users_updated: int = 0
    users_reactivated: int = 0
    users_deactivated: int = 0
    skipped_no_email: int = 0
    skipped_unmatched: int = 0
    skipped_malformed: int = 0
    app_user_index_size: int = 0              # size of the public.users email index
    deal_backfill_rows_updated: int = 0
    deal_backfill_per_op: dict[str, int]
    error_class: str | None = None
    error_message: str | None = None

    def to_metadata(self) -> dict[str, Any]: ...
```

## 13. Error Handling & Observability

Same three failure classes as phase 2. Key specifics:

- **No app users in the org.** `email_index` is empty; every owner becomes `skipped_unmatched`. Sync is considered successful (just trivially empty). INFO log notes the count.
- **All HubSpot owners unmatched.** Normal on small orgs. No warning spam — single INFO with summary counts.
- **Email collision between HubSpot owners.** Two owner rows with the same email → both would attempt to use the same `matched_app_user_id`. Only the FIRST wins; the second's attempted insert would collide on the PK. Writer detects and classifies the second as `skipped_dup_email` (WARNING log) without failing the sync. Rare in practice — HubSpot portals don't usually have two owner accounts for one email.
- **Sync history status values** — same as phase 2: only `SUCCESS | FAILED | PARTIAL`, no invented `TIMED_OUT`. Context in `sync_metadata`.

### Connection metadata shape

```json
{
  "record_sync": {
    "pipelines": { ... },
    "users": {
      "status": "success",
      "last_synced_at": "2026-04-23T10:00:00Z",
      "fetched_owners": 87,
      "users_created": 3,
      "users_updated": 4,
      "users_reactivated": 0,
      "users_deactivated": 0,
      "skipped_no_email": 0,
      "skipped_unmatched": 80,
      "skipped_malformed": 0,
      "app_user_index_size": 12,
      "deal_backfill_rows_updated": 0,
      "deal_backfill_per_op": { "owner_id": 0, "created_by_user_id": 0, "updated_by_user_id": 0 },
      "error_class": null,
      "error_message": null
    }
  }
}
```

## 14. Write-Side Isolation

- Every `crm_users` statement filters by `(organisation_id, integration_id, is_deleted=false)`.
- Email index query filters by `organisation_id` only (app users are org-scoped by their own FK).
- Deal backfill filters per op: `(organisation_id, integration_id, is_deleted=false, <external_col>=:external_id)` — inherited from the backfill contract.

Cross-tenant leakage is impossible by construction: neither the email index nor any persistence statement crosses org boundaries.

## 15. Testing (single end-phase task)

**Unit (pure, no DB, no network):**

- `normalize_one_owner` — happy, missing id (skip), missing email (valid but flagged), teams empty, no primary team, unparseable createdAt, archived-slipped-through defensive drop.
- Email normalization — mixed case, whitespace, unicode, empty string, None.
- `DealFkBackfillOp` new shape (`email_col`) — FK+name+email passes; FK+email without name rejected; allowlist rejects unknown col; mapping tuple shape enforced.
- `build_hubspot_initial_sync_chain` regression — chain now contains schema → pipelines → users → finalize in order; link_error unchanged.

**Component (in-memory DB):**

- `HubSpotUserWriter.apply()` — insert new, update existing, reactivate inactive, deactivate missing, refuse cross-integration writes. Email-duplicate owner → second one classified and logged, sync continues.
- Email index builder — empty-org path; multi-user path; case-insensitive match.
- `backfill_crm_deals` with the new 4-tuple shape (FK + name + email) — covers `owner_id` + `owner_name` + `owner_email` op.

**Integration (mocked HubSpot HTTP):**

- `HubSpotOwnersFetcher` — single-page, multi-page cursor pagination, 401-refresh, 429 retry, malformed payload (non-list results).
- `HubSpotUserRecordSyncService.run()` — end-to-end with fixture HubSpot owners + fixture app users; asserts counts in `UserSyncResult` + rows in `crm_users` + `CRMDeal` backfill side-effects.

**Worker:**

- `sync_hubspot_users_task` — success; configuration error re-raises; timeout re-raises (regression guard); history + metadata writes.
- Post-connect — HubSpot metadata at connect time now includes `record_sync.users.status="queued"`.

**Regression:**

- Phase-2 tests continue to pass; chain composition test updated to expect 4 positional links (was 3).

## 16. Open Decisions Resolved

- **Match scoping?** Same-org only. Cross-org would leak `user_id`s across tenants.
- **Email normalization?** `strip().lower()` both sides. SQL compares on `LOWER(email) = :normalized`.
- **Null email?** Skipped (`skipped_no_email`).
- **Unmatched owners?** Skipped. **No `crm_users` row is created.** Reactivation on later sync happens only if the person signs up as an app user in the interim.
- **Re-match on later sync?** No. `crm_users.user_id` is immutable (PK + FK target). Already-persisted rows are never re-evaluated for match.
- **Duplicate HubSpot owners with same email?** First wins; second is `skipped_dup_email` WARNING-logged. Does not fail the sync.
- **Backfill display fields for unmatched owners?** **No** this phase. Future phase-6 deal sync may denormalize name/email directly from the deal API's associated-owner expansion.
- **Contract extension?** `DealFkBackfillOp` gains `email_col` + 4-tuple mapping. Backwards-compatible for existing shapes (new column defaults to None).
- **Chain re-ordering?** No. Append users between pipelines and finalize. Phases 4-8 append above finalize in their own PRs.
- **Late-signup drift handling?** Not in this phase. Future signup-flow change — see §2 strategic context.
- **Tests in plan?** Single end-phase task.

## 17. Documentation Updates After Implementation

- `src/workers/integrations_worker/README.md` — mention the users task and its chain position.
- `docs/Technical Documentation/integrations/architecture-overview.md` — phase 3 live.
- `docs/Technical Documentation/integrations/sync-by-provider.md` — HubSpot row: users sync supported, matched-only rule documented.

## 18. Implementation Readiness

Sequenced steps:

1. Extend `DealFkBackfillOp` shape (new `email_col`, 4-tuple mapping, updated validator + SQL builder).
2. `records/users/user_types.py` — `NormalizedHubSpotOwner`, `OwnerMatchResult`, `UserWriteResult`, `UserSyncResult`.
3. `records/users/user_normalizer.py` — pure normalization + email-index helper.
4. `providers/crm/hubspot/owners_fetcher.py` — paginated fetch with token retry.
5. `records/users/user_writer.py` — matched-only upsert + active-snapshot deactivation (+ dup-email classification).
6. `records/users/user_sync_service.py` — fresh-session orchestrator, loads email index in Session A.
7. Tier 3 task `hubspot_user_record_sync_task.py` — same shape as pipelines task, re-raises on timeout, never touches `initial_sync_status`.
8. Register task: `config.py` `TASK_LIMITS` + `task_routes`; `tasks/crm/__init__.py` + `tasks/__init__.py`.
9. Append to `hubspot_chain.py` between pipelines and finalize.
10. Update `InitialSyncService.apply_post_connect_state` metadata.
11. Extend `send_hubspot_deal_schema_sync.py` with `--users-only` flag.
12. Documentation updates per §17.
13. Tests — single end-phase task covering §15.
