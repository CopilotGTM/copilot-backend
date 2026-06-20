# CRM Stage And Owner Service Boundary Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define the production boundary for deal stage transitions and deal owner assignment before continuing the deal-service refactor.

**Architecture:** Stage is deal-owned because it changes deal lifecycle state, stage timestamps, stage history, and workflow-triggerable deal mutation events. Owner assignment is also deal-owned as a business action, but it must synchronize a `CRMDealParticipant` row with `participant_type=user` and `relationship_role=owner` so the deal roster, notes mentions, meetings, and future workflow tools see the owner consistently. The participant row is a projection/relationship record; `CRMDeal.owner_id`, `owner_name`, and `owner_email` remain the fast deal snapshot and filter surface.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async sessions, PostgreSQL tenant schemas, pytest, mypy, ruff.

---

## Current Repo State

Current files already exist:

```text
src/service/crm/deals/stage_service.py
src/service/crm/deals/owner_service.py
src/service/crm/deals/write_service.py
src/service/crm/participants/participant_write_service.py
```

Current observations:

- `DealStageService.apply_stage()` exists and mutates a loaded `CRMDeal` plus `CRMDealStageHistory`.
- `DealOwnerService.apply_owner()` exists and mutates `CRMDeal.owner_id`, `owner_name`, and `owner_email`.
- `DealWriteService.move_stage()` still owns the public stage operation and still has stage-history helper methods.
- `DealWriteService.assign_owner()` still owns the public owner operation.
- `DealWriteService.create_deal()` resolves the owner snapshot directly and does not yet route owner participant sync through `DealOwnerService`.
- Owner assignment currently updates the deal owner snapshot but does not yet guarantee a matching `CRMDealParticipant` user row with `relationship_role=owner`.
- `DealReadService.get_deal_team_members_batch()` explicitly excludes user participants where `relationship_role = owner`, which means owner participants can exist separately from normal team members.
- `ParticipantWriteService.update_participant_role()` updates participant role metadata only; it does not mutate `CRMDeal.owner_*`, which is the correct raw participant boundary.
- Current checkout must not be implemented directly on `main`; create an isolated feature branch/worktree before code changes.

## Core Decision

### Stage Service Belongs Under Deals

Keep stage logic here:

```text
src/service/crm/deals/stage_service.py
```

Reason:

- Stage is a property of a deal, not a participant, contact, account, task, or activity.
- Stage changes drive `CRMDeal.stage_id`, `stage_name`, `stage_entered_at`, and `stage_updated_at`.
- Stage changes create `CRMDealStageHistory` rows.
- Stage changes will trigger workflows through `crm.deal.stage_changed`.
- Stage changes are part of deal lifecycle analytics such as stage duration and conversion.

So the production boundary is:

```text
DealWriteService / DealLifecycleService
  -> DealStageService
  -> CRMDeal + CRMDealStageHistory
  -> DealMutationEventService
```

### Owner Assignment Belongs Under Deals, But Syncs Participants

Keep owner assignment logic here:

```text
src/service/crm/deals/owner_service.py
```

But the owner service must call participant writing:

```text
DealOwnerService
  -> InternalCRMUserService.ensure_crm_user()
  -> update CRMDeal owner snapshot
  -> ParticipantWriteService.add_participants(
       participant_type=user,
       related_id=owner_user_id,
       relationship_role=owner
     )
  -> DealMutationEventService
```

Reason:

- The owner assignment is a deal aggregate action because it changes the deal's owner snapshot and owner filters.
- The owner is also a person attached to the deal, so the roster should include a `USER` participant row.
- Moving owner assignment entirely into `participants/` would make the participant service responsible for `CRMDeal.owner_id`, owner filters, and owner workflow events. That is the wrong direction.
- Keeping the owner only on `CRMDeal` would make participants/team/mentions incomplete.

Final rule:

```text
CRMDeal.owner_id/name/email = authoritative deal owner snapshot.
CRMDealParticipant(type=user, relationship_role=owner) = roster projection of the owner.
```

Role matching rule:

```text
The owner relationship role is compared by normalized role key:
strip -> lowercase -> spaces become underscores.

"owner", " Owner ", and "OWNER" are all the owner projection role.
```

## Why Not Put Owner Entirely In Participants?

It sounds attractive because owner is a person on a deal, but it creates hidden bugs:

1. Deal list filtering uses `CRMDeal.owner_id`, not participants.
2. Owner update workflows need a clear deal mutation event: `crm.deal.owner_changed`.
3. Owner assignment can clear or replace owner, which is a deal-level invariant.
4. Participant rows can represent collaborators, split owners, and watchers; not every user participant is the owner.
5. If participants own owner assignment, then every participant role update could accidentally become a deal owner update.

So participants should store the relationship row, but deals own the owner business rule.

## Target Stage Flow

```text
API / agent / bot asks to move stage
  -> DealWriteService.move_stage(deal_id, stage_id)
  -> load internal deal with organisation_id + internal integration guard
  -> load target CRMDealStage with organisation_id + pipeline + integration guard
  -> DealStageService.apply_stage(deal, stage, source)
       if same stage:
         return changed=false
       else:
         update deal stage snapshot
         create CRMDealStageHistory
         return changed_fields
  -> DealMutationEventService.record_event(
       event_type=crm.deal.stage_changed,
       changed_fields=stage_id/stage_name
     )
  -> caller commits
```

Expected behavior:

- Same-stage requests are no-ops.
- No new stage history row is created for a no-op.
- Stage validation stays org-scoped and integration-scoped.
- Stage history is written in the same DB transaction as the deal update.
- Workflow dispatch is not run here. It will consume mutation events later.

## Target Owner Flow

```text
API / agent / bot assigns owner
  -> DealWriteService.assign_owner(deal_id, owner_user_id)
  -> load internal deal with organisation_id + internal integration guard
  -> if owner_user_id is present:
       InternalCRMUserService.ensure_crm_user(owner_user_id)
     else:
       crm_user = None
  -> DealOwnerService.apply_owner(deal, crm_user)
       update deal owner snapshot
       return changed_fields
  -> if changed and crm_user exists:
       ParticipantWriteService.add_participants(
         deal_id,
         [{participant_type=user, related_id=owner_user_id, relationship_role=owner}]
       )
  -> if changed and owner cleared:
       soft-delete or demote active owner participant row
  -> DealMutationEventService.record_event(
       event_type=crm.deal.owner_changed,
       changed_fields=owner_id/owner_name/owner_email
     )
  -> caller commits
```

Expected behavior:

- Same-owner assignment is a no-op.
- Clearing owner clears the deal snapshot and removes/demotes the owner participant relationship.
- Owner participant rows are excluded from normal deal-team list responses unless the response explicitly includes owner details.
- Non-owner user participants remain untouched.
- The owner workflow trigger comes from the deal mutation event, not from raw participant writes.

## Target Create Deal Owner Flow

```text
API / agent / bot creates deal with owner_user_id
  -> DealWriteService.create_deal(payload)
  -> create CRMDeal row
  -> if owner_user_id is present:
       InternalCRMUserService.ensure_crm_user(owner_user_id)
       DealOwnerService.apply_owner(deal, crm_user)
       DealOwnerService.sync_owner_participant(deal, crm_user)
  -> create initial CRMDealStageHistory
  -> DealMutationEventService.record_event(
       event_type=crm.deal.created,
       changed_fields includes owner_id/owner_name/owner_email when provided
     )
  -> caller commits
```

Expected behavior:

- A deal created with an owner has both the deal owner snapshot and owner participant projection before commit.
- A deal created without an owner does not create an owner participant row.
- If participant sync fails during create, the create operation fails and rolls back with the deal row.
- Create emits one `crm.deal.created` event, not a separate `crm.deal.owner_changed` event for the initial owner.

## Data Ownership Contract

| Data | Owner | Reason |
| --- | --- | --- |
| `CRMDeal.stage_id` | Deals | Primary deal state |
| `CRMDeal.stage_name` | Deals | Fast deal list/detail display |
| `CRMDeal.stage_entered_at` | Deals | Current-stage analytics |
| `CRMDealStageHistory` | Deals | Stage duration and lifecycle history |
| `CRMDeal.owner_id` | Deals | Deal filter, assignment state, workflow trigger |
| `CRMDeal.owner_name` | Deals | Snapshot for fast response rendering |
| `CRMDeal.owner_email` | Deals | Snapshot for filters, mentions, display |
| `CRMDealParticipant(type=user, role=owner)` | Participants, orchestrated by deals | Roster representation of the owner |
| Non-owner user participants | Participants | Collaborators, split owners, watchers |

## Service Responsibilities

### `DealStageService`

Should own:

- apply a loaded stage to a loaded deal
- update current stage timestamps
- create `CRMDealStageHistory`
- return `DealStageChangeResult`
- no-op detection

Should not own:

- API request parsing
- loading arbitrary deals from untrusted IDs
- workflow dispatch
- lifecycle close/reopen rules
- commits

### `DealOwnerService`

Should own:

- applying a resolved `CRMUser` to a loaded deal
- clearing owner fields
- returning `DealOwnerChangeResult`
- comparing old/new owner snapshot fields

Should orchestrate or expose a method for:

- ensuring owner participant row exists
- clearing/demoting old owner participant row
- syncing owner participant projection during both create and assign-owner flows

Should not own:

- raw participant duplicate rules
- participant DTO mapping
- API response shaping
- commits

### `ParticipantWriteService`

Should own:

- creating/restoring `CRMDealParticipant`
- resolving `USER` participants through `InternalCRMUserService`
- role-specific uniqueness for non-contact participants
- internal CRM deal guard
- soft-deleting or restoring participant rows when explicitly asked by deal-owned orchestration

Should not own:

- deciding whether a user participant means deal owner
- updating `CRMDeal.owner_id`
- emitting `crm.deal.owner_changed`

## Loopholes To Close

### Loophole 1: Owner Snapshot And Owner Participant Drift Apart

Risk:

```text
CRMDeal.owner_id = user_a
but CRMDealParticipant(role=owner) points to user_b or does not exist.
```

Required behavior:

```text
DealOwnerService updates both in one transaction.
If participant sync fails, the owner assignment fails and caller rolls back.
```

Resolution:

```text
DealOwnerService exposes owner-projection helpers that call ParticipantWriteService.
The public DealWriteService create/assign-owner methods call those helpers before
flush/commit returns. No separate session or post-commit job is used.
```

### Loophole 2: Owner Participant Appears Twice In Deal Team

Risk:

```text
Owner is returned once through owner_details and again in deal_team.
```

Required behavior:

```text
Deal team reads keep excluding relationship_role=owner.
Owner appears through owner_details.
If a future UI wants full roster, add an explicit include_owner_participant option.
```

Resolution:

```text
Read filtering must use the normalized owner role key, not a case-sensitive
string comparison. Add a regression with owner role casing/whitespace plus a
normal collaborator on the same deal.
```

### Loophole 3: Participant Role Update Accidentally Changes Deal Owner

Risk:

```text
PATCH participant role to owner starts changing CRMDeal.owner_id.
```

Required behavior:

```text
Raw ParticipantWriteService role updates do not mutate deal owner.
Only DealOwnerService can create or clear the authoritative owner assignment.
```

Resolution:

```text
Keep ParticipantWriteService.update_participant_role() deal-snapshot neutral.
Add a regression proving changing a raw user participant role to owner does not
change CRMDeal.owner_id/name/email.
```

### Loophole 4: Stage Service Emits Duplicate Mutation Events

Risk:

```text
DealStageService emits crm.deal.stage_changed and DealWriteService emits another event.
```

Required behavior:

```text
Stage service returns changed_fields only.
The public write orchestrator emits one mutation event for the user action.
```

### Loophole 5: Lifecycle Close/Reopen Bypasses Stage Service

Risk:

```text
close_deal(stage_id=closed_won_stage) directly mutates stage_id and skips stage history.
```

Required behavior:

```text
LifecycleService must use DealStageService whenever lifecycle changes stage.
```

### Loophole 6: No-Op Owner/Stage Writes Pollute Audit

Risk:

```text
Assigning the same owner or moving to the same stage creates history and mutation rows.
```

Required behavior:

```text
No-op returns changed=false.
No mutation event.
No stage history row.
No timestamp churn except if product explicitly wants touched_at semantics later.
```

Resolution:

```text
DealWriteService must return before touching source_modified_at/synced_at/last_seen_at
when DealStageService or DealOwnerService reports changed=false.
```

### Loophole 7: Create Deal Owner Bypasses Owner Projection

Risk:

```text
create_deal(owner_user_id=user_a) sets CRMDeal.owner_id=user_a
but does not create CRMDealParticipant(type=user, role=owner).
```

Required behavior:

```text
Create-deal owner handling uses the same owner projection path as assign-owner.
Initial owner is part of crm.deal.created changed_fields; no extra owner_changed
event is emitted for creation.
```

Resolution:

```text
DealWriteService.create_deal() must call DealOwnerService after the deal row
exists and before returning. Add a test that creates a deal with owner_user_id
and asserts exactly one owner participant exists.
```

### Loophole 8: Clearing Owner Leaves A Stale Owner Participant

Risk:

```text
assign_owner(deal_id, None) clears CRMDeal.owner_id
but leaves an active CRMDealParticipant(type=user, role=owner).
```

Required behavior:

```text
Clearing owner soft-deletes only active owner-role user participants for the deal.
Non-owner user participants remain active.
```

Resolution:

```text
DealOwnerService must provide a clear-owner projection helper. It should query
active USER participants for the deal using the normalized owner role key and
set is_deleted=true plus sync timestamps on matching rows only.
```

### Loophole 9: Owner Replacement Leaves Multiple Active Owner Rows

Risk:

```text
assign_owner(deal_id, user_b) creates/restores owner participant for user_b
but leaves user_a's owner participant active.
```

Required behavior:

```text
At most one active owner-role USER participant exists per internal deal after
DealOwnerService completes.
```

Resolution:

```text
After ensuring the new owner participant, DealOwnerService soft-deletes any
other active owner-role USER participants for that deal in the same transaction.
```

### Loophole 10: Case-Sensitive Owner Role Comparisons

Risk:

```text
relationship_role="Owner" or " owner " leaks into deal_team because read filters
only compare relationship_role != "owner".
```

Required behavior:

```text
Owner role checks are normalized consistently in read filtering and write
cleanup helpers.
```

Resolution:

```text
Use a shared local helper or identical expression equivalent to:
lower(replace(trim(coalesce(relationship_role, "")), " ", "_")) == "owner".
Do not change stored role casing for existing rows unless the row is being
created/restored by owner assignment.
```

### Loophole 11: Lifecycle No-Op Still Pollutes Audit

Risk:

```text
close_deal() or reopen_deal() resolves a stage equal to the current stage and
still creates lifecycle mutation noise even when the lifecycle fields are
already in the requested state.
```

Required behavior:

```text
Lifecycle operations may emit lifecycle events for real lifecycle field changes,
but they must not create stage history or stage semantic noise when the stage is
unchanged.
```

Resolution:

```text
Lifecycle code uses DealStageService.apply_stage() and only includes stage
changed_fields from its result when changed=true. Existing close/reopen behavior
can still update close/reopen metadata when lifecycle fields genuinely change.
```

### Loophole 12: Implementation Starts From Main

Risk:

```text
Code changes happen directly on main while this plan file is still untracked.
```

Required behavior:

```text
Implementation starts from an isolated feature branch or sibling worktree.
The plan document is committed with the implementation or staged explicitly as
part of the branch.
```

Resolution:

```text
Before Task 1, create/switch to a feature branch such as
codex/crm-stage-owner-service-boundaries and confirm git status.
```

## Implementation Tasks

### Task 1: Harden Stage Service Boundary

**Files:**

- Modify: `src/service/crm/deals/stage_service.py`
- Modify: `src/service/crm/deals/write_service.py`
- Test: `tests/service/crm/test_deal_stage_service.py`
- Existing test: `tests/service/crm/test_deal_write_service_stage_history.py`

- [x] Add focused tests proving same-stage is a no-op.
- [x] Add focused tests proving stage history is created only when stage changes.
- [x] Move remaining duplicate stage-history helper behavior out of `DealWriteService`.
- [x] Ensure `DealWriteService.move_stage()` delegates to `DealStageService.apply_stage()`.
- [x] Ensure only `DealWriteService.move_stage()` records `crm.deal.stage_changed`.
- [x] Ensure no-op stage calls return before timestamp or mutation-event writes.

Verification:

```bash
uv run pytest tests/service/crm/test_deal_stage_service.py tests/service/crm/test_deal_write_service_stage_history.py -q
```

### Task 2: Make Owner Service Participant-Aware

**Files:**

- Modify: `src/service/crm/deals/owner_service.py`
- Modify: `src/service/crm/deals/write_service.py`
- Test: `tests/service/crm/test_deal_owner_service.py`
- Existing participant tests: `tests/service/crm/test_deal_participant_service_internal_ids.py`

- [x] Add tests proving assign-owner updates deal snapshot.
- [x] Add tests proving create-deal with owner also creates/restores `USER` owner participant.
- [x] Add tests proving assign-owner creates/restores `USER` participant with `relationship_role=owner`.
- [x] Add tests proving clear-owner clears/demotes only owner participant rows.
- [x] Add tests proving owner replacement leaves only the new owner participant active.
- [x] Add tests proving normal user participants are not modified.
- [x] Add tests proving raw participant role updates to owner do not mutate `CRMDeal.owner_*`.
- [x] Ensure same-owner assignment is a no-op.
- [x] Ensure owner assignment records only one `crm.deal.owner_changed` mutation event.
- [x] Ensure create-deal records initial owner only inside `crm.deal.created`.

Verification:

```bash
uv run pytest tests/service/crm/test_deal_owner_service.py tests/service/crm/test_deal_participant_service_internal_ids.py -q
```

### Task 3: Protect Read Shape

**Files:**

- Modify only if needed: `src/service/crm/deals/read_service.py`
- Test: `tests/service/crm/test_deals_access_service_team_members.py`

- [x] Keep `get_deal_team_members_batch()` excluding owner participants.
- [x] Add a test where a deal has both:
  - owner participant with `relationship_role=owner`
  - owner participant with non-canonical casing/spacing such as ` Owner `
  - collaborator participant with `relationship_role=collaborator`
- [x] Assert only collaborator appears in `deal_team`.
- [x] Keep `owner_details` sourced from `CRMDeal.owner_*`, not from participant rows.

Verification:

```bash
uv run pytest tests/service/crm/test_deals_access_service_team_members.py -q
```

## Recommended Order

Do this order:

1. Confirm branch/worktree isolation before the first code edit.
2. Stage service hardening.
3. Owner service participant sync.
4. Read-shape protection.

Reason:

- Branch isolation is required before implementation because the current checkout may be `main`.
- Stage is isolated and deal-only, so it is lower-risk.
- Owner touches deal plus participants, so it needs the participant boundary to be clear first.
- Read-shape tests should be last because they validate the full owner projection behavior.

## Success Criteria

- Stage transitions live in `DealStageService`.
- `DealWriteService.move_stage()` is orchestration only.
- Same-stage updates do not create history or mutation events.
- Owner assignment lives in `DealOwnerService`.
- Owner assignment updates `CRMDeal.owner_*` and synchronizes a `USER` participant row.
- Create-deal with `owner_user_id` uses the same owner participant projection path.
- Clearing/replacing owner leaves no stale active owner-role user participants.
- Raw participant role updates do not become owner assignment.
- Owner participant is excluded from ordinary deal-team response with normalized owner-role matching.
- `crm.deal.stage_changed` and `crm.deal.owner_changed` are each emitted once per real user action.
- No provider sync writers are changed.
- Implementation does not happen directly on `main`.
