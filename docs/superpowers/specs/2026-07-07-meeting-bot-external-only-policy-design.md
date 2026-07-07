# Meeting Bot External-Only Policy Design

## Recommendation

Use one organisation-level meeting bot policy first. Do not add per-user controls in the first version.

Reason: CopilotGTM sends one company-branded bot for a meeting, not one named bot per user. If three internal users are invited and one user enables the bot while two users disable it, the meeting still gets the same organisation bot and the other two users cannot tell whose setting caused it. For a sales product, that ambiguity is worse than a single organisation-owned rule. The policy gives the company a clear consent model and keeps external buyer calls recordable without creating hidden per-user conflicts.

The policy values should be:

- `all`: auto-join all eligible meetings.
- `external_only`: auto-join only eligible meetings with at least one attendee outside the organisation domains.
- `never`: never auto-join. Manual invite should also be blocked unless product explicitly wants a break-glass admin action later.

Recommended product default is `external_only`, including existing organisations with no saved policy.

Decision on 2026-07-07: backend defaults and enforcement stay `external_only` now. The setting is exposed through the tenant-user preferences API. For now any authenticated organisation user can change it and the value applies to the whole organisation; later this can be restricted to tenant admins.

## Existing System

Current meeting bot dispatch already has the right final control point:

- `MeetingRecordingLifecycleService.dispatch_event()` owns the provider side effect.
- `MeetingRecordingEligibilityService.evaluate_for_scheduling()` already centralizes the basic schedule checks.
- `recording_window.py` keeps the three-day dispatch window shared.
- `MeetingRecordingSessionService` owns durable `meeting_recording_sessions` rows.
- `CalendarEvent.conference_link` is the join URL contract.
- `CalendarEvent.attendees`, `CalendarEvent.organiser_email`, and `Organisation.domain` contain enough data to classify internal vs external meetings.
- `MeetingsTabService._is_external_meeting()` already computes `is_external` for UI rows from attendee email domains and organisation domains.
- User-facing preference routes already live under `/users/preference` and use authenticated tenant-user context.

The implementation should extend these existing pieces rather than create a new workflow runtime.

## Policy Contract

Store the org policy in master DB:

```json
{
  "meeting_recording": {
    "bot_join_policy": "external_only"
  }
}
```

Allowed values:

| Value | Meaning |
| --- | --- |
| `all` | The bot may auto-join any otherwise eligible meeting. |
| `external_only` | The bot may auto-join only if at least one attendee email domain is outside `Organisation.domain`. |
| `never` | The bot must not auto-join or manual-join from user surfaces. |

Expose the options as code-owned master data, not a tenant table:

```json
{
  "meeting_recording": {
    "bot_join_policy_options": [
      {"value": "external_only", "label": "External meetings only"},
      {"value": "all", "label": "All meetings"},
      {"value": "never", "label": "Never join"}
    ]
  }
}
```

This is returned from the tenant-user meeting recording preference response as frontend metadata. It is not persisted into `public.organisations.preferences`.

## External Meeting Definition

A meeting is external when at least one attendee email domain is not in the organisation's configured domains.

Rules:

- Normalize domains to lowercase and strip a leading `www.`.
- Compare attendee email domains against `public.organisations.domain`.
- Ignore attendee rows without a valid email address.
- Treat aliases listed in `Organisation.domain` as internal.
- Do not use the calendar provider type, meeting platform, title, or CRM deal link as the external signal.
- For `external_only`, if the organisation has no configured domains, fail closed and do not auto-join.
- Organizer-only meetings with no attendee rows are not external.

The existing UI helper already follows most of this. The backend dispatch path should get a reusable equivalent so the provider side effect does not depend on UI formatting.

## Effective Behavior

The lifecycle service should evaluate bot dispatch in this order:

1. Calendar event exists.
2. Calendar event is not cancelled.
3. Active calendar integration still exists.
4. Meeting has a `conference_link`.
5. Meeting is inside the three-day recording window.
6. Organisation policy permits the meeting.
7. Durable session row is created or reused.
8. Recall provider bot is created or reconciled.

Policy outcomes:

| Policy | Internal meeting | External meeting |
| --- | --- | --- |
| `all` | Join if otherwise eligible. | Join if otherwise eligible. |
| `external_only` | Skip with `meeting_recording_policy_external_only`. | Join if otherwise eligible. |
| `never` | Skip with `meeting_recording_policy_never`. | Skip with `meeting_recording_policy_never`. |

Manual invite behavior should use the same policy. Otherwise a user can bypass admin policy from the Meetings tab or agent tool.

## Why Not User-Level First

User-level preferences sound useful, but they do not match the current one-bot-per-meeting model.

Problems:

- The bot appears with company branding, not the requesting user's name.
- Multiple internal attendees can have conflicting preferences for the same event.
- The current session table is keyed around one event/provider session, not one requesting user per event.
- A sales admin is usually the right owner of call-recording consent, especially for customer-facing meetings.

If user-level control is required later, add it only as a stricter preference under the org ceiling:

| Organisation policy | User policy | Effective behavior |
| --- | --- | --- |
| `never` | any | never |
| `external_only` | `all` | external_only |
| `external_only` | `external_only` | external_only |
| `all` | `external_only` | external_only |
| `all` | `never` | never |
| `all` | `all` | all |

That means users can make bot joining stricter for their own single-user manual flows, but cannot weaken the organisation rule. Do not use this for auto-dispatch until the product defines conflict resolution for meetings with multiple internal users.

## Backend Changes

Minimal implementation path:

1. Add policy constants and normalization in `src/service/orgnisation/organisation_preferences.py`.
2. Extend `UpdateOrganisationAppSettingsRequest` with `meeting_recording.bot_join_policy`.
3. Merge and normalize `preferences.meeting_recording.bot_join_policy` in `merge_organisation_app_settings()`.
4. Return `bot_join_policy_options` from `GET /users/preference/meeting-recording` so the frontend can render a dedicated configuration control without hardcoding the enum.
5. Add a reusable external-meeting classifier near the meeting-bot service layer that loads org domains and attendee emails.
6. Keep `MeetingRecordingLifecycleService.dispatch_event()` as the final policy gate and call the policy check before session creation and provider calls.
7. Leave `MeetingRecordingEligibilityService` focused on schedule/window primitives unless the lifecycle service is explicitly refactored to delegate to it.
8. Apply the same effective check to manual invite surfaces:
   - `MeetingsTabService.invite_bot()`
   - agent `send_meeting_bot_invite`
   - `CRMMeetingRecordingControlService.invite_bot()`
9. Keep cancellation behavior policy-independent. If a bot already exists and the event is cancelled, cleanup must still run.
10. Include the policy in meeting list/detail bot status so frontend can disable actions with a concrete reason.

Do not add a new workflow table or background scheduler. Existing producers can keep enqueueing tasks; the lifecycle service should remain the final gate, and producers can later add early skips to reduce queue noise.

## Frontend Settings Contract

The organisation settings surface should show a single setting:

Label: `Meeting bot auto-join`

Options:

- `External meetings only` - recommended default.
- `All meetings`.
- `Never join`.

The frontend should read the current value and allowed values from:

```http
GET /users/preference/meeting-recording
```

Saving should PATCH:

```http
PATCH /users/preference/meeting-recording
```

```json
{
  "bot_join_policy": "external_only"
}
```

This is intentionally tenant-user scoped, not CopilotGTM application-admin scoped. The persisted value still lives in `public.organisations.preferences.meeting_recording.bot_join_policy`, so a change by one user affects the whole organisation.

The Meetings tab should continue to show per-meeting bot state, but disabled invite actions should surface backend reasons:

- `meeting_recording_policy_external_only`
- `meeting_recording_policy_never`
- existing reasons such as `missing_meeting_url`, `meeting_already_started`, and `meeting_outside_recording_window`

## Migration And Rollout

No new table is required.

Rollout decision: privacy-first. Missing policy normalizes to `external_only` for every organisation. If customer success confirms specific existing customers were sold "record all meetings" behavior, backfill only those approved organisations to `all`; new organisations still default to `external_only`.

## Testing

Smallest useful tests:

- Preference normalization accepts `all`, `external_only`, and `never`.
- Invalid or missing policy normalizes to the chosen default.
- External classifier returns true for at least one attendee outside org domains.
- External classifier returns false for all-internal attendees.
- External classifier returns false when org domains are missing.
- Lifecycle dispatch skips internal meetings under `external_only` before creating a session row.
- Lifecycle dispatch skips all meetings under `never` before creating a session row.
- Lifecycle dispatch still allows external meetings under `external_only`.
- Manual invite is blocked by `never` and by internal meeting plus `external_only`.
- Cancellation of an already scheduled bot still works even if policy later changes to `never`.

## Resolved Product Decision

Missing policy on existing organisations means `external_only`. The backend enforces that now, and the frontend can expose the dedicated `Meeting bot auto-join` setting through `GET/PATCH /users/preference/meeting-recording`.
