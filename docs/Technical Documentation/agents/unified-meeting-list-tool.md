# Unified Meeting List Tool Design

## Summary

Replace the current split between `list_deal_meetings` and `list_meetings_by_date`
with one agent-facing meeting listing tool that is time-window based and scoped to
the current user's own connected calendar.

The tool should answer both:

- "List my next 2 meetings"
- "Show meetings for this deal today"

without exposing meetings from another user's connected calendar or requiring the
agent to choose between two overlapping listing tools.

## Current State

Today we have two overlapping tools:

- `list_deal_meetings`: deal-scoped, returns meetings attached to a deal.
- `list_meetings_by_date`: date-scoped, returns current-user meetings across deals.

The split causes avoidable routing ambiguity. The date tool also joins through
`CalendarEventDealAssociation`, so it only sees meetings already attached to a deal.
That is too narrow for "my calendar" queries.

## Proposed Tool Contract

Expose one public tool named `list_meetings`.

```python
async def list_meetings_tool(
    start_datetime: str | None = None,
    end_datetime: str | None = None,
    limit: int = 10,
    deal_id: str | None = None,
    attendee_email: str | None = None,
    attendee_name: str | None = None,
    has_transcript: bool | None = None,
    has_summary: bool | None = None,
) -> list[dict[str, Any]]:
    ...
```

Defaults:

- `start_datetime`: defaults to the agent context anchor/current time.
- `end_datetime`: optional. If omitted, use open-ended listing ordered by soonest start time.
- `limit`: defaults to 10, hard max 50.
- `deal_id`: optional narrowing filter.
- `has_transcript` / `has_summary`: kept for meeting-intelligence status queries
  such as "show meetings with transcripts" or "which meetings are missing
  summaries." Revisit these filters after usage is clearer; normal calendar
  listing should not depend on them.

Expose one public side-effect tool named `send_meeting_bot_invite`.

```python
async def send_meeting_bot_invite_tool(
    event_id: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    ...
```

Behavior:

- If `event_id` is omitted, resolve the next meeting from the current user's
  connected calendar and return `confirmation_required` unless `confirmed=True`.
- If `event_id` is provided, verify the event belongs to the current user's
  connected calendar before dispatching.
- Dispatch uses `MeetingRecordingLifecycleService.schedule_for_event(...)`, so
  existing provider/session behavior such as `already_sent`, reschedule repair,
  missing join URL, and started-meeting skips remains centralized.
- The tool never sends a bot from org-wide calendar lookup or attendee email
  alone.

Agent examples:

- "List my next 2 meetings" -> `start_datetime=now`, `limit=2`.
- "List my next meetings" -> `start_datetime=now`, `limit=10`.
- "Meetings today" -> local day start/end from the user's timezone context.
- "Meetings between 2 and 5" -> exact local window converted to UTC.
- "Meetings for the Acme deal" -> same tool with `deal_id`.

## Security Boundary

The tool must only return meetings from the current user's connected calendar.
Do not rely on attendee email alone as the ownership boundary.

Required query boundary:

1. Use `context.organisation_id` and `context.user_id` as immutable boundaries.
2. Resolve active calendar integrations where:
   - `KnowledgeIntegration.organisation_id == context.organisation_id`
   - `KnowledgeIntegration.created_by_user_id == context.user_id`
   - `KnowledgeIntegration.is_active == True`
   - `KnowledgeIntegration.integration_type IN (GOOGLE_CALENDAR, OUTLOOK_CALENDAR)`
3. Join `CalendarEventUserState` through those integration IDs:
   - `CalendarEventUserState.integration_id == KnowledgeIntegration.integration_id`
   - `CalendarEventUserState.is_deleted == False`
4. Join to `CalendarEvent` and always filter:
   - `CalendarEvent.organisation_id == context.organisation_id`
   - event overlaps the requested time window.

This means a user can only list meetings that came from their own calendar
integration, even if another user's meeting is in the same organisation or has
similar attendees.

If `context.user_id` is missing, return an empty list and log a warning. Do not
fall back to org-wide listing.

## Query Behavior

Use `CalendarEvent` as the primary source, not `CalendarEventDealAssociation`.
Deal context should be attached when available.

Base query:

- Start from active user calendar integrations.
- Join `CalendarEventUserState`.
- Join `CalendarEvent`.
- Left join active `CalendarEventDealAssociation`.
- Left join non-deleted `CRMDeal`.
- Left join `Meeting` for transcript status.

Filtering:

- With `end_datetime`: include events where `start_at < end_datetime` and `end_at > start_datetime`.
- Without `end_datetime`: include events where `start_at >= start_datetime`.
- With `deal_id`: require active association for that deal.
- With attendee filters: join `CalendarAttendee` using case-insensitive comparisons.
- Apply `has_transcript` and `has_summary` before `LIMIT`.

Sorting:

- Open-ended upcoming listing: `start_at ASC`.
- Bounded windows: `start_at ASC` unless the user asks for recent/past behavior through the prompt layer.

Output fields:

- `event_id`, `title`, `start_at`, `end_at`, `duration_minutes`
- `status`, `location`, `conference_link`, `online_meeting_provider`
- `organiser_name`, `organiser_email`
- `attendee_count`, `attendees` capped to a small list
- `deal` object when an active deal association exists
- `has_transcript`, `has_summary`, `has_prep`

Do not expose internal integration IDs, user-state IDs, or raw provider payloads.

## Migration Plan

Implementation should be incremental:

1. Add a shared query/service function for the unified list behavior.
2. Add the new `list_meetings` agent tool and register it for `GeneralAgent` and `VoiceRealtimeAgent`.
3. Keep `list_deal_meetings` and `list_meetings_by_date` available for specialized
   deal/summary/briefing workflows until those agent contracts are reviewed.
4. Update voice realtime tool manifest to expose the unified contract.
5. Update prompt routing to describe one meeting-listing path.
6. Keep old tool names out of the normal General/Voice agent tool selection.

## Tests

Required focused tests:

- `list_meetings` returns only events synced by the current user's active calendar integration.
- Events from another user's integration in the same organisation are excluded.
- Missing `context.user_id` returns `[]`.
- `limit` defaults to 10 and is capped at 50.
- Open-ended "next meetings" uses `start_datetime` plus limit without requiring an end time.
- Bounded windows include overlapping events, not only events whose start is inside the window.
- Deal filter returns only meetings associated with that deal while still enforcing user-calendar ownership.
- Case-insensitive attendee filtering works.
- Transcript/summary filters are applied before limiting.
- Registry exposes only the unified meeting-list tool for the primary chat agent once wrappers are no longer needed.

## Open Decisions

Default choices for implementation:

- Keep `end_datetime` optional.
- Use `CalendarEventUserState` plus `KnowledgeIntegration.created_by_user_id` as the ownership boundary.
- Include calendar events without deal associations for "my meetings" queries.
- Keep old tool names as wrappers for one release window to avoid breaking prompt/tool references.

## Meeting Bot Resolution Notes

The user does not need to provide a calendar event ID. For natural language
requests like "send a bot to my meeting with Saksham", the agent should first
call `list_meetings`, identify the best matching event by title/attendees/time,
then pass the internal `event_id` to `send_meeting_bot_invite`.

`send_meeting_bot_invite` sends to exactly one calendar event per call. If
`list_meetings` returns multiple plausible matches, the agent must ask the user
to choose one meeting before dispatching the bot.

Handled by chaining:

- "Send a bot to my meeting with Saksham" -> list meetings, match attendee/title,
  then send to the matched `event_id`.
- "Send a bot to my next Acme meeting" -> resolve/search the deal if needed,
  call `list_meetings(deal_id=...)`, then send to the matched `event_id`.
- "Send a bot to my next meeting" -> `send_meeting_bot_invite` can infer the
  next meeting but must return a confirmation preview before dispatch.

Genuine gaps:

- Multiple matching meetings still require user choice; the send tool does not
  choose between several candidate events.
- Meetings outside the listing window can be missed unless the agent expands
  `start_datetime`, `end_datetime`, or `limit`.
- Unsynced calendar events cannot be found because ownership is enforced through
  `CalendarEventUserState`.
- Events without conference links cannot receive a bot.
- Bulk send and bot cancellation are not supported by this tool.
