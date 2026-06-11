# Provider-Agnostic Meeting Join Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure Kai can be invited to supported online meetings regardless of whether the calendar event was synced from Outlook/Microsoft or Google Calendar.

**Architecture:** Keep `CalendarEvent.conference_link` as the single downstream join-url contract. Add provider-agnostic join-link extraction during calendar normalization, infer `online_meeting_provider` from the actual URL host, and preserve the existing meeting-bot dispatch contract. Current-user meeting visibility remains scoped through provider-parent `KnowledgeIntegration` rows because `CalendarEventUserState.integration_id` records the integration that synced the event.

**Tech Stack:** Python 3.11, FastAPI service layer, SQLModel/PostgreSQL, Celery dispatch, existing calendar sync normalizer/writer/change-detector, pytest.

---

## Confirmed Problem

The dev sample below proves the current failure mode.

Environment:

```text
kubectl context: copilot-gtm-aks-dev
namespace: copilotgtm
pod/deploy: deploy/copilot-backend
```

User and org:

```text
email = nikhil@copilotgtm.com
user_id = 5be25739-f5cd-4040-af0a-b5719e9c03fe
organisation_id = 1982306b-12a9-4e4b-9e6d-6996df7b1698
organisation_name = CopilotGTM
```

Affected event:

```text
event_id = 453a87cf-fa89-4b2c-92ac-0e1d5f3b837c
title = Meet with Carlos (Saksham Bhutani)
provider = outlook
integration_type = microsoft
start_at = 2026-06-11 17:00:00+00:00
organiser_email = cmunoz@avaya.com
conference_link = null
online_meeting_provider = null
meeting_recording_sessions row = none
```

Raw Outlook payload contains the actual Google Meet URL:

```text
calendar_events.location = https://meet.google.com/yqz-yaqx-dup
data_payload.locations[0].displayName = https://meet.google.com/yqz-yaqx-dup
data_payload.locations[0].uniqueId = https://meet.google.com/yqz-yaqx-dup
```

Root cause:

- `CalendarEventNormalizer._apply_microsoft_online_meeting()` currently reads only `onlineMeeting.joinUrl`.
- For this Outlook invite, `onlineMeeting` and `onlineMeetingUrl` are null.
- The actual Google Meet URL is in Outlook location fields.
- Because `conference_link` stays null, all downstream paths correctly skip:
  - Meetings tab invite button: `MeetingsTabService._can_invite_bot()`
  - Calendar sync callback dispatch: `CalendarSyncService._queue_recording_dispatch_if_eligible()`
  - Final send gate: `MeetingRecordingDispatchService.dispatch_event()`
  - Fallback scan: `MeetingRecordingDispatchService._find_pending_recording_event_ids()`

## Requirements

1. Treat calendar provider and meeting platform as separate concepts.
2. Support these combinations through the same persisted `conference_link` contract:
   - Outlook calendar event with Google Meet link
   - Outlook calendar event with Zoom link
   - Outlook calendar event with Teams link
   - Outlook calendar event with Webex link
   - Google Calendar event with Google Meet link
   - Google Calendar event with Zoom link
   - Google Calendar event with Teams link
   - Google Calendar event with Webex link
3. Do not store arbitrary URLs as meeting join links.
4. Unwrap Microsoft Safe Links only when the decoded `url=` target is a supported meeting host.
5. Do not add new meeting-bot dispatch branches by calendar provider.
6. Preserve org isolation and current-user scoping.
7. Preserve the provider-parent ownership model for user-visible meeting queries.
8. Backfill existing upcoming events whose raw provider payload contains a supported join link but `conference_link` is null.
9. Queue recording tasks only after the backfill transaction commits.
10. Make backfill runbook commands Kubernetes-context explicit.

## Architecture Decisions

- `CalendarEvent.conference_link` remains the only join URL used by Recall dispatch, manual invite, fallback scans, and UI join actions.
- `CalendarEvent.online_meeting_provider` records the actual meeting platform, not the calendar provider.
- `CalendarEvent.provider` records the calendar source, such as `google`, `microsoft`, or `outlook`.
- `CalendarEventUserState.integration_id` remains provider-parent provenance for current-user meeting ownership checks.
- Dispatch service can keep accepting parent and calendar capability integration types for backwards compatibility.
- Current-user surfaces should keep provider-parent filters unless a later migration proves user-state rows are written against capability rows.

## File Structure

- Modify: `src/service/integrations/calendar/normalizer.py`
  - Owns provider payload parsing. Add public join-link extraction helpers, HTML URL cleanup, Microsoft Safe Link unwrapping, and platform classification here.
- Modify: `tests/service/integrations/calendar/test_normalizer.py`
  - Adds regression tests for Outlook/Google mixed-provider join links, structured Google add-on links, unsupported URLs, lookalike hosts, HTML-escaped query strings, and trailing prose cleanup.
- Modify: `tests/service/integrations/test_calendar_sync_service_incremental.py`
  - Adds the regression that proves the calendar callback path updates a previously skipped event and queues meeting recording dispatch after commit.
- Create: `src/service/integrations/calendar/integration_types.py`
  - Centralizes calendar integration type tuples so Meetings tab, CRM meetings, agent tools, and dispatch do not drift.
- Modify: `src/service/meeting_bot/dispatch_service.py`
  - Import the broad dispatch tuple from `integration_types.py`.
- Modify: `src/service/meetings_tab_service.py`
  - Import the provider-parent visibility tuple from `integration_types.py`.
- Modify: `src/service/crm/access/meetings_access_service.py`
  - Import the provider-parent visibility tuple from `integration_types.py`.
- Modify: `src/agents/tools/intelligence/send_meeting_bot_invite.py`
  - Import the provider-parent visibility tuple as string values from `integration_types.py`.
- Modify: `src/agents/tools/intelligence/list_meetings_unified.py`
  - Import the provider-parent visibility tuple as string values from `integration_types.py`.
- Modify: `tests/service/test_meetings_tab_service.py`
  - Lock that Meetings tab uses provider-parent visibility types and still renders cross-provider links.
- Modify: `tests/service/crm/test_crm_meetings_access_service.py`
  - Lock that CRM meetings access uses the same provider-parent visibility tuple.
- Modify: `tests/agents/tools/intelligence/test_send_meeting_bot_invite.py`
  - Update the existing constants test to use the shared provider-parent string tuple.
- Create: `scripts/maintenance/calendar/backfill_meeting_join_links.py`
  - Dry-run/apply maintenance script for existing rows. Prints bounded samples and queues tasks after commit.
- Create: `tests/scripts/maintenance/calendar/test_backfill_meeting_join_links.py`
  - Unit tests for extraction, sample output shape, row-count based updates, and post-commit queue behavior.
- Modify: `docs/Technical Documentation/integrations/sync-by-provider.md`
  - Document the provider-agnostic meeting-link contract and the provider-parent ownership boundary.

## Supported Hosts

Host matching rules:

- Exact match allowed for `meet.google.com`, `teams.microsoft.com`, `teams.live.com`.
- Exact or subdomain match allowed for Zoom and Webex, e.g. `zoom.us`, `acme.zoom.us`, `webex.com`, `acme.webex.com`.
- Reject lookalikes such as `zoom.evil.example`, `meet.google.com.evil.example`, and `teams.microsoft.com.evil.example`.
- Treat `*.safelinks.protection.outlook.com` as a wrapper only. Decode its `url=` query parameter and then apply the same supported-host rules to the decoded target.

Provider classification:

```text
meet.google.com -> googleMeet
teams.microsoft.com / teams.live.com -> teamsForBusiness
zoom.us / *.zoom.us -> zoom
webex.com / *.webex.com -> webex
```

## Task 1: Add Shared Join-Link Extraction Tests

**Files:**
- Modify: `tests/service/integrations/calendar/test_normalizer.py`

- [ ] **Step 1: Add failing tests for Microsoft/Outlook mixed-provider links**

Append these tests to `tests/service/integrations/calendar/test_normalizer.py`:

```python
def test_microsoft_normalizer_extracts_google_meet_from_location() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Meet with Carlos",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "location": {"displayName": "https://meet.google.com/yqz-yaqx-dup"},
            "locations": [
                {
                    "displayName": "https://meet.google.com/yqz-yaqx-dup",
                    "uniqueId": "https://meet.google.com/yqz-yaqx-dup",
                    "locationType": "default",
                    "uniqueIdType": "private",
                }
            ],
            "organizer": {"emailAddress": {"address": "cmunoz@avaya.com", "name": "Carlos"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert normalized.fields["conference_link"] == "https://meet.google.com/yqz-yaqx-dup"
    assert normalized.fields["online_meeting_provider"] == "googleMeet"
```

```python
def test_microsoft_normalizer_extracts_zoom_from_locations() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer Zoom",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "locations": [
                {
                    "displayName": "Zoom",
                    "uniqueId": "https://acme.zoom.us/j/123456789",
                    "locationType": "default",
                    "uniqueIdType": "private",
                }
            ],
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert normalized.fields["conference_link"] == "https://acme.zoom.us/j/123456789"
    assert normalized.fields["online_meeting_provider"] == "zoom"
```

```python
def test_microsoft_normalizer_extracts_teams_from_body_when_online_meeting_missing() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer Teams",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "body": {
                "contentType": "html",
                "content": '<a href="https://teams.microsoft.com/l/meetup-join/abc">Join</a>',
            },
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert normalized.fields["conference_link"] == "https://teams.microsoft.com/l/meetup-join/abc"
    assert normalized.fields["online_meeting_provider"] == "teamsForBusiness"
```

```python
def test_microsoft_normalizer_extracts_webex_from_body_preview() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer Webex",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "bodyPreview": "Join Webex https://acme.webex.com/meet/buyer.",
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert normalized.fields["conference_link"] == "https://acme.webex.com/meet/buyer"
    assert normalized.fields["online_meeting_provider"] == "webex"
```

- [ ] **Step 2: Add failing tests for Google Calendar fallback and structured add-on links**

Append these tests to the same file:

```python
def test_google_normalizer_extracts_zoom_from_description_when_conference_data_missing() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Zoom",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "description": "Join Zoom Meeting https://acme.zoom.us/j/987654321",
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://acme.zoom.us/j/987654321"
    assert normalized.fields["online_meeting_provider"] == "zoom"
```

```python
def test_google_normalizer_extracts_teams_from_location() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Teams",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "location": "https://teams.live.com/meet/123",
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://teams.live.com/meet/123"
    assert normalized.fields["online_meeting_provider"] == "teamsForBusiness"
```

```python
def test_google_normalizer_extracts_webex_from_description() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Webex",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "description": "Join Webex: https://webex.com/meet/customer)",
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://webex.com/meet/customer"
    assert normalized.fields["online_meeting_provider"] == "webex"
```

```python
def test_google_normalizer_classifies_structured_zoom_addon_from_entry_point_uri() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Zoom",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "conferenceData": {
                "conferenceId": "987654321",
                "conferenceSolution": {"key": {"type": "addOn"}, "name": "Zoom Meeting"},
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://acme.zoom.us/j/987654321"}
                ],
            },
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://acme.zoom.us/j/987654321"
    assert normalized.fields["online_meeting_provider"] == "zoom"
    assert normalized.fields["online_meeting_id"] == "987654321"
```

```python
def test_google_normalizer_prefers_structured_google_meet_over_description_link() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Meet",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "description": "Backup link https://acme.zoom.us/j/987654321",
            "conferenceData": {
                "conferenceId": "abc-defg-hij",
                "conferenceSolution": {"key": {"type": "hangoutsMeet"}},
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
                ],
            },
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://meet.google.com/abc-defg-hij"
    assert normalized.fields["online_meeting_provider"] == "googleMeet"
    assert normalized.fields["online_meeting_id"] == "abc-defg-hij"
```

- [ ] **Step 3: Add failing tests for unsafe and malformed URL inputs**

Append these tests to the same file:

```python
def test_normalizer_rejects_unsupported_location_url() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer call",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "location": {"displayName": "https://example.com/join"},
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert "conference_link" not in normalized.fields
    assert "online_meeting_provider" not in normalized.fields
```

```python
def test_normalizer_rejects_lookalike_hosts() -> None:
    for url in (
        "https://zoom.evil.example/j/123456789",
        "https://meet.google.com.evil.example/abc-defg-hij",
        "https://teams.microsoft.com.evil.example/l/meetup-join/abc",
    ):
        normalized = CalendarEventNormalizer().normalize(
            {
                "id": "ms-user-event",
                "iCalUId": f"shared-ms-ical-{url}",
                "subject": "Customer call",
                "isCancelled": False,
                "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
                "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
                "location": {"displayName": url},
                "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
                "attendees": [],
            },
            "microsoft",
        )

        assert "conference_link" not in normalized.fields
        assert "online_meeting_provider" not in normalized.fields
```

```python
def test_normalizer_extracts_url_without_trailing_prose() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Meet",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "description": "https://meet.google.com/abc-defg-hij Join with Google Meet",
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://meet.google.com/abc-defg-hij"
    assert normalized.fields["online_meeting_provider"] == "googleMeet"
```

```python
def test_google_normalizer_rejects_unsupported_structured_addon_url() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Unsupported add-on",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "conferenceData": {
                "conferenceId": "vendor-123",
                "conferenceSolution": {"key": {"type": "addOn"}, "name": "Unknown Vendor"},
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meetings.example.com/join/123"}
                ],
            },
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert "conference_link" not in normalized.fields
    assert "online_meeting_provider" not in normalized.fields
    assert "online_meeting_id" not in normalized.fields
```

```python
def test_normalizer_unescapes_html_query_parameters() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer Zoom",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "body": {
                "contentType": "html",
                "content": (
                    '<a href="https://acme.zoom.us/j/123456789?pwd=abc&amp;from=addon">'
                    "Join Zoom</a>"
                ),
            },
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert (
        normalized.fields["conference_link"]
        == "https://acme.zoom.us/j/123456789?pwd=abc&from=addon"
    )
    assert normalized.fields["online_meeting_provider"] == "zoom"
```

```python
def test_normalizer_unwraps_microsoft_safelink_to_supported_meeting_url() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer Zoom",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "body": {
                "contentType": "html",
                "content": (
                    "Join: "
                    "https://nam12.safelinks.protection.outlook.com/?url="
                    "https%3A%2F%2Facme.zoom.us%2Fj%2F123456789%3Fpwd%3Dabc"
                    "&amp;data=05%7C02%7Copaque"
                ),
            },
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert (
        normalized.fields["conference_link"]
        == "https://acme.zoom.us/j/123456789?pwd=abc"
    )
    assert normalized.fields["online_meeting_provider"] == "zoom"
```

- [ ] **Step 4: Run tests and verify failure**

Run:

```bash
uv run pytest tests/service/integrations/calendar/test_normalizer.py -v
```

Expected:

```text
FAILED tests/service/integrations/calendar/test_normalizer.py::test_microsoft_normalizer_extracts_google_meet_from_location
FAILED tests/service/integrations/calendar/test_normalizer.py::test_microsoft_normalizer_extracts_zoom_from_locations
FAILED tests/service/integrations/calendar/test_normalizer.py::test_google_normalizer_extracts_zoom_from_description_when_conference_data_missing
FAILED tests/service/integrations/calendar/test_normalizer.py::test_google_normalizer_classifies_structured_zoom_addon_from_entry_point_uri
FAILED tests/service/integrations/calendar/test_normalizer.py::test_google_normalizer_rejects_unsupported_structured_addon_url
FAILED tests/service/integrations/calendar/test_normalizer.py::test_normalizer_unescapes_html_query_parameters
FAILED tests/service/integrations/calendar/test_normalizer.py::test_normalizer_unwraps_microsoft_safelink_to_supported_meeting_url
```

Some rejection tests may pass immediately if no URL is extracted today.

## Task 2: Implement Provider-Agnostic Join-Link Extraction

**Files:**
- Modify: `src/service/integrations/calendar/normalizer.py`
- Test: `tests/service/integrations/calendar/test_normalizer.py`
- Test: `tests/service/integrations/test_calendar_sync_service_incremental.py`

- [ ] **Step 1: Add imports and constants**

In `src/service/integrations/calendar/normalizer.py`, replace the current single `import urllib.parse` with:

```python
import html
import re
import urllib.parse
```

Add these constants under `DEFAULT_EVENT_TITLE`:

```python
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")

EXACT_MEETING_HOSTS = {
    "meet.google.com",
    "teams.microsoft.com",
    "teams.live.com",
}

EXACT_MEETING_PROVIDERS = {
    "meet.google.com": "googleMeet",
    "teams.microsoft.com": "teamsForBusiness",
    "teams.live.com": "teamsForBusiness",
}

SUFFIX_MEETING_HOSTS = {
    "zoom.us",
    "webex.com",
}

MICROSOFT_SAFELINK_HOST_SUFFIX = ".safelinks.protection.outlook.com"
```

- [ ] **Step 2: Add public extraction helper and shared private helpers**

Add these methods inside `CalendarEventNormalizer` before `_extract_google_event_fields`:

```python
    @classmethod
    def extract_join_link_update(
        cls,
        provider: str | None,
        event_data: dict[str, Any],
    ) -> dict[str, str] | None:
        provider_name = (provider or "").lower()
        if provider_name in {"microsoft", "outlook"}:
            candidates = cls._microsoft_join_link_candidates(event_data)
        elif provider_name == "google":
            candidates = cls._google_join_link_candidates(event_data)
        else:
            candidates = cls._generic_join_link_candidates(event_data)

        fields: dict[str, Any] = {}
        cls._apply_join_link_from_candidates(fields, candidates, replace_existing=True)
        conference_link = fields.get("conference_link")
        online_meeting_provider = fields.get("online_meeting_provider")
        if not isinstance(conference_link, str) or not isinstance(online_meeting_provider, str):
            return None
        return {
            "conference_link": conference_link,
            "online_meeting_provider": online_meeting_provider,
        }

    @classmethod
    def _apply_join_link_from_candidates(
        cls,
        fields: dict[str, Any],
        candidates: Iterable[Any],
        *,
        replace_existing: bool = False,
    ) -> None:
        if fields.get("conference_link") and not replace_existing:
            return
        for candidate in candidates:
            meeting_url = cls._extract_supported_meeting_url(candidate)
            if meeting_url is None:
                continue
            provider = cls._meeting_provider_from_url(meeting_url)
            if provider is None:
                continue
            fields["conference_link"] = meeting_url
            fields["online_meeting_provider"] = provider
            return

    @classmethod
    def _extract_supported_meeting_url(cls, value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        for raw_url in cls._iter_urls(value):
            cleaned = cls._clean_candidate_url(raw_url)
            candidate = cls._unwrap_microsoft_safelink(cleaned) or cleaned
            if cls._meeting_provider_from_url(candidate):
                return candidate
        return None

    @staticmethod
    def _iter_urls(value: str) -> Iterable[str]:
        for match in URL_PATTERN.finditer(value):
            yield match.group(0)

    @staticmethod
    def _clean_candidate_url(value: str) -> str:
        return html.unescape(value.strip().rstrip(").,;]"))

    @staticmethod
    def _unwrap_microsoft_safelink(value: str) -> str | None:
        try:
            parsed = urllib.parse.urlparse(value)
            host = (parsed.hostname or "").lower()
            if not host.endswith(MICROSOFT_SAFELINK_HOST_SUFFIX):
                return None
            encoded_urls = urllib.parse.parse_qs(parsed.query).get("url", [])
            if not encoded_urls:
                return None
            return encoded_urls[0]
        except Exception:
            return None

    @staticmethod
    def _meeting_provider_from_url(value: str) -> str | None:
        try:
            parsed = urllib.parse.urlparse(value)
        except Exception:
            return None

        if parsed.scheme not in {"http", "https"}:
            return None

        host = (parsed.hostname or "").lower()
        if host in EXACT_MEETING_HOSTS:
            return EXACT_MEETING_PROVIDERS[host]
        for suffix_host in SUFFIX_MEETING_HOSTS:
            if host == suffix_host or host.endswith(f".{suffix_host}"):
                return "zoom" if suffix_host == "zoom.us" else "webex"
        return None
```

- [ ] **Step 3: Add candidate collection helpers**

Add these methods inside `CalendarEventNormalizer` near `_apply_microsoft_online_meeting`:

```python
    @staticmethod
    def _microsoft_join_link_candidates(event_data: dict[str, Any]) -> Iterable[Any]:
        online_meeting = event_data.get("onlineMeeting")
        if isinstance(online_meeting, dict):
            yield online_meeting.get("joinUrl")

        yield event_data.get("onlineMeetingUrl")

        location = event_data.get("location")
        if isinstance(location, dict):
            yield location.get("displayName")
            yield location.get("locationUri")
        else:
            yield location

        locations = event_data.get("locations")
        if isinstance(locations, list):
            for item in locations:
                if not isinstance(item, dict):
                    continue
                yield item.get("displayName")
                yield item.get("locationUri")
                yield item.get("uniqueId")

        body = event_data.get("body")
        if isinstance(body, dict):
            yield body.get("content")
        yield event_data.get("bodyPreview")

    @staticmethod
    def _google_join_link_candidates(event_data: dict[str, Any]) -> Iterable[Any]:
        yield event_data.get("hangoutLink")
        yield event_data.get("location")
        yield event_data.get("description")

    @staticmethod
    def _generic_join_link_candidates(event_data: dict[str, Any]) -> Iterable[Any]:
        yield event_data.get("conference_link")
        yield event_data.get("location")
        yield event_data.get("description")
        yield event_data.get("bodyPreview")
```

- [ ] **Step 4: Update Microsoft conference extraction**

Replace `_apply_microsoft_online_meeting()` with:

```python
    @classmethod
    def _apply_microsoft_online_meeting(
        cls,
        fields: dict[str, Any],
        event_data: dict[str, Any],
    ) -> None:
        cls._apply_join_link_from_candidates(
            fields,
            cls._microsoft_join_link_candidates(event_data),
        )

        join_url = fields.get("conference_link")
        if not isinstance(join_url, str):
            return

        if fields.get("online_meeting_provider") == "teamsForBusiness":
            meeting_id = cls.extract_teams_meeting_id(join_url)
            if meeting_id:
                fields["online_meeting_id"] = meeting_id
```

- [ ] **Step 5: Update Google structured conference extraction**

Replace `_apply_google_conference()` with:

```python
    @classmethod
    def _apply_google_conference(cls, fields: dict[str, Any], conference_data: Any) -> None:
        if not isinstance(conference_data, dict):
            return

        video_uri = None
        for entry in conference_data.get("entryPoints", []):
            if entry.get("entryPointType") == "video":
                video_uri = entry.get("uri")
                break

        solution = conference_data.get("conferenceSolution", {})
        solution_type = solution.get("key", {}).get("type", "")

        if solution_type == "hangoutsMeet":
            if not isinstance(video_uri, str):
                return
            meeting_url = cls._extract_supported_meeting_url(video_uri)
            if meeting_url is None:
                return
            fields["conference_link"] = meeting_url
            fields["online_meeting_provider"] = "googleMeet"
            fields["online_meeting_id"] = conference_data.get("conferenceId")
            return

        if isinstance(video_uri, str):
            meeting_url = cls._extract_supported_meeting_url(video_uri)
            if meeting_url is None:
                return
            fields["conference_link"] = meeting_url
            provider = cls._meeting_provider_from_url(meeting_url)
            if provider is None:
                return
            fields["online_meeting_provider"] = provider
            fields["online_meeting_id"] = conference_data.get("conferenceId")
```

This deliberately does not preserve the old `addOn` provider-name fallback. A Google structured add-on link is persisted only when its video URI points at a supported host; otherwise the fields stay unset and downstream dispatch correctly skips.

- [ ] **Step 6: Update Google extraction to use fallback candidates**

In `_extract_google_event_fields()`, after:

```python
        cls._apply_google_conference(fields, event_data.get("conferenceData"))
```

add:

```python
        cls._apply_join_link_from_candidates(
            fields,
            cls._google_join_link_candidates(event_data),
        )
```

This preserves structured Google Meet priority because `_apply_join_link_from_candidates()` returns immediately when `conference_link` already exists.

- [ ] **Step 7: Add calendar sync dispatch regression test**

Append this test to `tests/service/integrations/test_calendar_sync_service_incremental.py` inside `TestCalendarIncrementalBatchWrites`:

```python
    @pytest.mark.asyncio
    async def test_incremental_sync_enqueues_outlook_event_when_join_link_is_newly_extracted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The backend callback path must stop skipping Outlook events with third-party links."""
        organisation_id = uuid4()
        integration = _make_integration(
            organisation_id=organisation_id,
            integration_type=IntegrationType.MICROSOFT,
        )
        session = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()
        service = CalendarSyncService(session, organisation_id)
        service._sync_history.start = AsyncMock(return_value=SimpleNamespace())
        service._sync_history.complete = AsyncMock()
        existing = _event_row("shared-ms-ical")
        existing.start_at = datetime.now(UTC) + timedelta(hours=2)
        existing.end_at = existing.start_at + timedelta(minutes=45)
        existing.conference_link = None
        service._load_existing_events_by_identity = AsyncMock(
            return_value={("shared-ms-ical", ""): existing}
        )

        async def _batch_upsert_events(_integration, items):
            assert len(items) == 1
            normalized = items[0].event
            assert normalized.fields["conference_link"] == "https://meet.google.com/yqz-yaqx-dup"
            assert normalized.fields["online_meeting_provider"] == "googleMeet"
            existing.conference_link = normalized.fields["conference_link"]
            existing.online_meeting_provider = normalized.fields["online_meeting_provider"]
            return {("shared-ms-ical", ""): existing}

        service._batch_upsert_events = AsyncMock(side_effect=_batch_upsert_events)
        service._batch_sync_attendees = AsyncMock()
        service._batch_upsert_user_states = AsyncMock()
        send_task = AsyncMock()
        commit_count = 0

        async def _commit() -> None:
            nonlocal commit_count
            commit_count += 1

        async def _send_task(*args, **kwargs):
            assert commit_count >= 1
            return await send_task(*args, **kwargs)

        session.commit.side_effect = _commit
        monkeypatch.setattr(
            "src.service.integrations.calendar.sync_service.send_deal_task_async",
            _send_task,
        )

        await service.sync_calendar_events_incremental(
            integration=integration,
            provider="microsoft",
            events=[
                {
                    "id": "ms-user-event",
                    "iCalUId": "shared-ms-ical",
                    "subject": "Meet with Carlos",
                    "isCancelled": False,
                    "lastModifiedDateTime": "2026-06-11T16:30:00Z",
                    "start": {
                        "dateTime": existing.start_at.isoformat().replace("+00:00", "Z"),
                        "timeZone": "UTC",
                    },
                    "end": {
                        "dateTime": existing.end_at.isoformat().replace("+00:00", "Z"),
                        "timeZone": "UTC",
                    },
                    "location": {"displayName": "https://meet.google.com/yqz-yaqx-dup"},
                    "organizer": {
                        "emailAddress": {"address": "cmunoz@avaya.com", "name": "Carlos"}
                    },
                    "attendees": [],
                }
            ],
        )

        service._batch_upsert_events.assert_awaited_once()
        send_task.assert_awaited_once_with(
            "deal.send_meeting_recording",
            kwargs={
                "organisation_id": str(organisation_id),
                "calendar_event_id": str(existing.event_id),
            },
            queue="deal",
            routing_key="deal",
        )
```

- [ ] **Step 8: Run normalizer and sync regression tests**

Run:

```bash
uv run pytest tests/service/integrations/calendar/test_normalizer.py tests/service/integrations/test_calendar_sync_service_incremental.py::TestCalendarIncrementalBatchWrites::test_incremental_sync_enqueues_outlook_event_when_join_link_is_newly_extracted -v
```

Expected:

```text
tests/service/integrations/calendar/test_normalizer.py ... PASSED
tests/service/integrations/test_calendar_sync_service_incremental.py::TestCalendarIncrementalBatchWrites::test_incremental_sync_enqueues_outlook_event_when_join_link_is_newly_extracted PASSED
```

- [ ] **Step 9: Commit**

Run:

```bash
git add src/service/integrations/calendar/normalizer.py tests/service/integrations/calendar/test_normalizer.py tests/service/integrations/test_calendar_sync_service_incremental.py
git commit -m "fix: extract provider agnostic calendar meeting links"
```

## Task 3: Centralize Calendar Integration Type Contracts

**Files:**
- Create: `src/service/integrations/calendar/integration_types.py`
- Modify: `src/service/meeting_bot/dispatch_service.py`
- Modify: `src/service/meetings_tab_service.py`
- Modify: `src/service/crm/access/meetings_access_service.py`
- Modify: `src/agents/tools/intelligence/send_meeting_bot_invite.py`
- Modify: `src/agents/tools/intelligence/list_meetings_unified.py`
- Modify: `tests/service/test_meetings_tab_service.py`
- Modify: `tests/service/crm/test_crm_meetings_access_service.py`
- Modify: `tests/agents/tools/intelligence/test_send_meeting_bot_invite.py`

- [ ] **Step 1: Add failing tests for the integration type contract**

In `tests/service/test_meetings_tab_service.py`, add:

```python
from src.service.integrations.calendar.integration_types import (
    DISPATCH_CALENDAR_INTEGRATION_TYPES,
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES,
)
from src.service.meetings_tab_service import CALENDAR_INTEGRATION_TYPES


def test_meetings_tab_uses_provider_parent_calendar_integrations() -> None:
    assert CALENDAR_INTEGRATION_TYPES == PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES
    assert CALENDAR_INTEGRATION_TYPES != DISPATCH_CALENDAR_INTEGRATION_TYPES
```

In `tests/service/crm/test_crm_meetings_access_service.py`, add:

```python
from src.service.crm.access.meetings_access_service import CALENDAR_INTEGRATION_TYPES
from src.service.integrations.calendar.integration_types import (
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES,
)


def test_crm_meetings_uses_provider_parent_calendar_integrations() -> None:
    assert CALENDAR_INTEGRATION_TYPES == PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES
```

Update `tests/agents/tools/intelligence/test_send_meeting_bot_invite.py` so `test_calendar_tool_filters_provider_parent_integrations()` becomes:

```python
from src.service.integrations.calendar.integration_types import (
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES,
)


def test_calendar_tool_filters_provider_parent_integrations() -> None:
    assert CALENDAR_INTEGRATION_TYPES == PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES
    assert LIST_MEETINGS_CALENDAR_INTEGRATION_TYPES == (
        PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES
    )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/service/test_meetings_tab_service.py::test_meetings_tab_uses_provider_parent_calendar_integrations tests/service/crm/test_crm_meetings_access_service.py::test_crm_meetings_uses_provider_parent_calendar_integrations tests/agents/tools/intelligence/test_send_meeting_bot_invite.py::test_calendar_tool_filters_provider_parent_integrations -v
```

Expected:

```text
ERROR ... No module named 'src.service.integrations.calendar.integration_types'
```

- [ ] **Step 3: Create shared integration type module**

Create `src/service/integrations/calendar/integration_types.py`:

```python
"""Shared calendar integration type contracts.

`CalendarEventUserState.integration_id` is written with the provider-parent
integration that synced the event. Current-user visibility checks should use
the provider-parent tuple. Dispatch keeps the broader tuple so old capability
rows, if present, remain eligible for fallback scheduling.
"""

from __future__ import annotations

from src.domain.models.organisation_tables.sync.integration_models import IntegrationType

PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE,
    IntegrationType.MICROSOFT,
)

PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES = tuple(
    integration_type.value for integration_type in PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES
)

DISPATCH_CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE,
    IntegrationType.MICROSOFT,
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)
```

- [ ] **Step 4: Update service and tool imports**

In `src/service/meeting_bot/dispatch_service.py`, replace the local `ACTIVE_CALENDAR_INTEGRATION_TYPES` tuple with:

```python
from src.service.integrations.calendar.integration_types import (
    DISPATCH_CALENDAR_INTEGRATION_TYPES as ACTIVE_CALENDAR_INTEGRATION_TYPES,
)
```

In `src/service/meetings_tab_service.py`, replace the local `CALENDAR_INTEGRATION_TYPES` tuple with:

```python
from src.service.integrations.calendar.integration_types import (
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES as CALENDAR_INTEGRATION_TYPES,
)
```

In `src/service/crm/access/meetings_access_service.py`, replace the local `CALENDAR_INTEGRATION_TYPES` tuple with:

```python
from src.service.integrations.calendar.integration_types import (
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES as CALENDAR_INTEGRATION_TYPES,
)
```

In both `src/agents/tools/intelligence/send_meeting_bot_invite.py` and `src/agents/tools/intelligence/list_meetings_unified.py`, replace the local string tuple with:

```python
from src.service.integrations.calendar.integration_types import (
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES as CALENDAR_INTEGRATION_TYPES,
)
```

Remove now-unused direct `IntegrationType` imports from files where the only remaining use was the deleted local tuple.

- [ ] **Step 5: Run focused integration type tests**

Run:

```bash
uv run pytest tests/service/test_meetings_tab_service.py::test_meetings_tab_uses_provider_parent_calendar_integrations tests/service/crm/test_crm_meetings_access_service.py::test_crm_meetings_uses_provider_parent_calendar_integrations tests/agents/tools/intelligence/test_send_meeting_bot_invite.py::test_calendar_tool_filters_provider_parent_integrations tests/service/meeting_bot/test_meeting_recording_dispatch_service.py::TestMeetingRecordingDispatchService::test_active_calendar_integration_types_include_parent_and_capability_rows -v
```

Expected:

```text
PASSED
```

- [ ] **Step 6: Commit**

Run:

```bash
git add src/service/integrations/calendar/integration_types.py src/service/meeting_bot/dispatch_service.py src/service/meetings_tab_service.py src/service/crm/access/meetings_access_service.py src/agents/tools/intelligence/send_meeting_bot_invite.py src/agents/tools/intelligence/list_meetings_unified.py tests/service/test_meetings_tab_service.py tests/service/crm/test_crm_meetings_access_service.py tests/agents/tools/intelligence/test_send_meeting_bot_invite.py
git commit -m "refactor: centralize calendar integration type contracts"
```

## Task 4: Add Existing-Row Backfill Script

**Files:**
- Create: `scripts/maintenance/calendar/backfill_meeting_join_links.py`
- Create: `tests/scripts/maintenance/calendar/test_backfill_meeting_join_links.py`

- [ ] **Step 1: Write extraction and post-commit queue tests**

Create `tests/scripts/maintenance/calendar/test_backfill_meeting_join_links.py`:

```python
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from scripts.maintenance.calendar.backfill_meeting_join_links import (
    BackfillCandidate,
    extract_join_link_update,
    format_sample,
    queue_recording_dispatches,
    queued_event_ids_for_updated_rows,
)


def test_extract_join_link_update_reads_outlook_location_google_meet() -> None:
    payload = {
        "location": {"displayName": "https://meet.google.com/yqz-yaqx-dup"},
        "locations": [
            {
                "displayName": "https://meet.google.com/yqz-yaqx-dup",
                "uniqueId": "https://meet.google.com/yqz-yaqx-dup",
            }
        ],
    }

    update = extract_join_link_update("outlook", payload)

    assert update == {
        "conference_link": "https://meet.google.com/yqz-yaqx-dup",
        "online_meeting_provider": "googleMeet",
    }


def test_extract_join_link_update_reads_google_description_zoom() -> None:
    payload = {
        "description": "Join Zoom Meeting https://acme.zoom.us/j/987654321",
    }

    update = extract_join_link_update("google", payload)

    assert update == {
        "conference_link": "https://acme.zoom.us/j/987654321",
        "online_meeting_provider": "zoom",
    }


def test_extract_join_link_update_rejects_unknown_url() -> None:
    payload = {
        "location": {"displayName": "https://example.com/join"},
    }

    assert extract_join_link_update("outlook", payload) is None


def test_format_sample_includes_operator_review_fields() -> None:
    candidate = BackfillCandidate(
        event_id=uuid4(),
        title="Meet with Carlos",
        start_at=datetime(2026, 6, 11, 17, 0, tzinfo=UTC),
        provider="outlook",
        conference_link="https://meet.google.com/yqz-yaqx-dup",
        online_meeting_provider="googleMeet",
    )

    sample = format_sample(candidate)

    assert sample["title"] == "Meet with Carlos"
    assert sample["provider"] == "outlook"
    assert sample["conference_host"] == "meet.google.com"
    assert sample["online_meeting_provider"] == "googleMeet"


def test_queued_event_ids_only_returns_updated_rows_inside_dispatch_window() -> None:
    now = datetime(2026, 6, 11, 16, 45, tzinfo=UTC)
    eligible = BackfillCandidate(
        event_id=uuid4(),
        title="Eligible",
        start_at=now + timedelta(minutes=15),
        provider="outlook",
        conference_link="https://meet.google.com/abc-defg-hij",
        online_meeting_provider="googleMeet",
    )
    too_late = BackfillCandidate(
        event_id=uuid4(),
        title="Too late",
        start_at=now - timedelta(minutes=1),
        provider="outlook",
        conference_link="https://meet.google.com/late-link",
        online_meeting_provider="googleMeet",
    )

    assert queued_event_ids_for_updated_rows([eligible, too_late], now) == [eligible.event_id]


@pytest.mark.asyncio
async def test_queue_recording_dispatches_continues_after_one_failure() -> None:
    organisation_id = uuid4()
    first_event_id = uuid4()
    second_event_id = uuid4()
    send_task = AsyncMock(side_effect=[RuntimeError("redis unavailable"), "task-2"])

    result = await queue_recording_dispatches(
        organisation_id,
        [first_event_id, second_event_id],
        send_task=send_task,
    )

    assert result.queued == 1
    assert result.queue_failed == 1
    assert result.failed_event_ids == (str(first_event_id),)
    assert send_task.await_count == 2
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/scripts/maintenance/calendar/test_backfill_meeting_join_links.py -v
```

Expected:

```text
ERROR ... ModuleNotFoundError: No module named 'scripts.maintenance.calendar.backfill_meeting_join_links'
```

- [ ] **Step 3: Create backfill script**

Create `scripts/maintenance/calendar/backfill_meeting_join_links.py`:

```python
"""Backfill supported meeting join links from stored calendar provider payloads.

Dry-run is the default. Use --apply only after reviewing counts and sample rows.
Recording tasks are queued only after the update transaction commits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import text

from src.infra.database.sessions import acquire_org_async_session
from src.service.integrations.calendar.normalizer import CalendarEventNormalizer
from src.service.meeting_bot.recording_window import is_within_recording_dispatch_window
from src.workers.deal_worker.client import send_deal_task_async

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackfillCandidate:
    event_id: UUID
    title: str | None
    start_at: datetime
    provider: str | None
    conference_link: str
    online_meeting_provider: str


@dataclass(frozen=True)
class BackfillSummary:
    scanned: int = 0
    matched: int = 0
    updated: int = 0
    queued: int = 0
    queue_failed: int = 0
    failed_event_ids: tuple[str, ...] = ()
    samples: tuple[dict[str, str | None], ...] = ()


@dataclass(frozen=True)
class QueueDispatchResult:
    queued: int = 0
    queue_failed: int = 0
    failed_event_ids: tuple[str, ...] = ()


def extract_join_link_update(
    provider: str | None,
    payload: dict[str, Any],
) -> dict[str, str] | None:
    return CalendarEventNormalizer.extract_join_link_update(provider, payload)


def format_sample(candidate: BackfillCandidate) -> dict[str, str | None]:
    parsed = urlparse(candidate.conference_link)
    return {
        "event_id": str(candidate.event_id),
        "title": candidate.title,
        "start_at": candidate.start_at.isoformat(),
        "provider": candidate.provider,
        "conference_host": parsed.hostname,
        "online_meeting_provider": candidate.online_meeting_provider,
    }


def queued_event_ids_for_updated_rows(
    updated_candidates: list[BackfillCandidate],
    now: datetime,
) -> list[UUID]:
    return [
        candidate.event_id
        for candidate in updated_candidates
        if is_within_recording_dispatch_window(candidate.start_at, now)
    ]


async def queue_recording_dispatches(
    organisation_id: UUID,
    event_ids: list[UUID],
    *,
    send_task: Callable[..., Awaitable[str]] = send_deal_task_async,
) -> QueueDispatchResult:
    queued = 0
    failed_event_ids: list[str] = []
    for event_id in event_ids:
        try:
            await send_task(
                "deal.send_meeting_recording",
                kwargs={
                    "organisation_id": str(organisation_id),
                    "calendar_event_id": str(event_id),
                },
                queue="deal",
                routing_key="deal",
            )
            queued += 1
        except Exception:
            failed_event_ids.append(str(event_id))
            logger.exception(
                "Failed to queue meeting recording dispatch during join-link backfill",
                extra={
                    "event": "meeting_join_link_backfill_queue_failed",
                    "organisation_id": str(organisation_id),
                    "calendar_event_id": str(event_id),
                },
            )

    return QueueDispatchResult(
        queued=queued,
        queue_failed=len(failed_event_ids),
        failed_event_ids=tuple(failed_event_ids),
    )


async def backfill_org(
    organisation_id: UUID,
    *,
    apply: bool = False,
    queue_recordings: bool = False,
    days_ahead: int = 30,
    limit: int = 500,
    event_id: UUID | None = None,
    sample_limit: int = 10,
) -> BackfillSummary:
    now = datetime.now(UTC)
    window_end = now + timedelta(days=days_ahead)
    scanned = 0
    matched_candidates: list[BackfillCandidate] = []
    updated_candidates: list[BackfillCandidate] = []
    queued_event_ids: list[UUID] = []
    samples: tuple[dict[str, str | None], ...] = ()

    async for session in acquire_org_async_session(organisation_id):
        event_filter = "and event_id = :event_id" if event_id else ""
        rows = (
            await session.execute(
                text(
                    f"""
                    select event_id, title, provider, data_payload, start_at
                    from calendar_events
                    where organisation_id = :organisation_id
                      and conference_link is null
                      and start_at > :now
                      and start_at < :window_end
                      and coalesce(status, '') != 'cancelled'
                      {event_filter}
                    order by start_at asc
                    limit :limit
                    """
                ),
                {
                    "organisation_id": organisation_id,
                    "now": now,
                    "window_end": window_end,
                    "event_id": event_id,
                    "limit": limit,
                },
            )
        ).mappings().all()
        scanned = len(rows)

        for row in rows:
            payload = row["data_payload"] or {}
            if not isinstance(payload, dict):
                continue
            update = extract_join_link_update(row["provider"], payload)
            if update is None:
                continue

            candidate = BackfillCandidate(
                event_id=row["event_id"],
                title=row["title"],
                start_at=row["start_at"],
                provider=row["provider"],
                conference_link=update["conference_link"],
                online_meeting_provider=update["online_meeting_provider"],
            )
            matched_candidates.append(candidate)

            if not apply:
                continue

            result = await session.execute(
                text(
                    """
                    update calendar_events
                    set conference_link = :conference_link,
                        online_meeting_provider = :online_meeting_provider,
                        updated_at = now()
                    where organisation_id = :organisation_id
                      and event_id = :event_id
                      and conference_link is null
                    """
                ),
                {
                    "organisation_id": organisation_id,
                    "event_id": candidate.event_id,
                    "conference_link": candidate.conference_link,
                    "online_meeting_provider": candidate.online_meeting_provider,
                },
            )
            if result.rowcount == 1:
                updated_candidates.append(candidate)

        if apply:
            await session.commit()

        if apply and queue_recordings:
            queued_event_ids = queued_event_ids_for_updated_rows(updated_candidates, now)

        sample_source = updated_candidates if apply else matched_candidates
        samples = tuple(
            format_sample(candidate) for candidate in sample_source[:sample_limit]
        )
        break

    queue_result = await queue_recording_dispatches(organisation_id, queued_event_ids)

    return BackfillSummary(
        scanned=scanned,
        matched=len(matched_candidates),
        updated=len(updated_candidates),
        queued=queue_result.queued,
        queue_failed=queue_result.queue_failed,
        failed_event_ids=queue_result.failed_event_ids,
        samples=samples,
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--organisation-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--queue-recordings", action="store_true")
    parser.add_argument("--days-ahead", type=int, default=30)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--event-id")
    parser.add_argument("--sample-limit", type=int, default=10)
    args = parser.parse_args()

    summary = await backfill_org(
        UUID(args.organisation_id),
        apply=args.apply,
        queue_recordings=args.queue_recordings,
        days_ahead=args.days_ahead,
        limit=args.limit,
        event_id=UUID(args.event_id) if args.event_id else None,
        sample_limit=args.sample_limit,
    )
    print(json.dumps(asdict(summary), default=str))


if __name__ == "__main__":
    asyncio.run(main())
```

The `queue_recording_dispatches()` call stays after the `async for session in acquire_org_async_session(...)` block. This keeps the org DB connection out of the Redis/Celery call path while still guaranteeing the update transaction has committed before workers can re-read the event. Per-event queue failures are logged and returned in the JSON summary instead of aborting later queue attempts after the database has already committed.

- [ ] **Step 4: Run script tests**

Run:

```bash
uv run pytest tests/scripts/maintenance/calendar/test_backfill_meeting_join_links.py -v
```

Expected:

```text
tests/scripts/maintenance/calendar/test_backfill_meeting_join_links.py ... PASSED
```

- [ ] **Step 5: Run dry-run in dev for the confirmed event**

Run:

```bash
kubectl --context copilot-gtm-aks-dev exec -n copilotgtm deploy/copilot-backend -- python -m scripts.maintenance.calendar.backfill_meeting_join_links --organisation-id 1982306b-12a9-4e4b-9e6d-6996df7b1698 --event-id 453a87cf-fa89-4b2c-92ac-0e1d5f3b837c --days-ahead 30 --limit 1
```

Expected:

```json
{"scanned": 1, "matched": 1, "updated": 0, "queued": 0, "queue_failed": 0, "failed_event_ids": [], "samples": [{"event_id": "453a87cf-fa89-4b2c-92ac-0e1d5f3b837c", "title": "Meet with Carlos (Saksham Bhutani)", "start_at": "2026-06-11T17:00:00+00:00", "provider": "outlook", "conference_host": "meet.google.com", "online_meeting_provider": "googleMeet"}]}
```

The sample must show `conference_host='meet.google.com'` and `online_meeting_provider='googleMeet'` while the `Meet with Carlos` row is still upcoming and unmodified.

- [ ] **Step 6: Apply in dev only after reviewing dry-run**

Run:

```bash
kubectl --context copilot-gtm-aks-dev exec -n copilotgtm deploy/copilot-backend -- python -m scripts.maintenance.calendar.backfill_meeting_join_links --organisation-id 1982306b-12a9-4e4b-9e6d-6996df7b1698 --event-id 453a87cf-fa89-4b2c-92ac-0e1d5f3b837c --days-ahead 30 --limit 1 --apply --queue-recordings
```

Expected:

```json
{"scanned": 1, "matched": 1, "updated": 1, "queued": 0, "queue_failed": 0, "failed_event_ids": [], "samples": [{"event_id": "453a87cf-fa89-4b2c-92ac-0e1d5f3b837c", "title": "Meet with Carlos (Saksham Bhutani)", "start_at": "2026-06-11T17:00:00+00:00", "provider": "outlook", "conference_host": "meet.google.com", "online_meeting_provider": "googleMeet"}]}
```

`queued` is `0` for the Carlos sample after its start time has passed. For future events still inside the recording dispatch window, expect `queued: 1`. If Redis/Celery queueing fails after the database commit, `queue_failed` is non-zero and `failed_event_ids` lists the event IDs that need a retry.

- [ ] **Step 7: Verify the confirmed event row**

Run a read-only query in the dev pod:

```bash
kubectl --context copilot-gtm-aks-dev exec -n copilotgtm deploy/copilot-backend -- python -c "import asyncio,json; from sqlalchemy import text; from src.infra.database.sessions import acquire_org_async_session; from uuid import UUID; org_id=UUID('1982306b-12a9-4e4b-9e6d-6996df7b1698'); event_id='453a87cf-fa89-4b2c-92ac-0e1d5f3b837c'; async def main():\n    async for s in acquire_org_async_session(org_id):\n        row=(await s.execute(text('select event_id, conference_link, online_meeting_provider from calendar_events where organisation_id=:organisation_id and event_id=:event_id'), {'organisation_id': org_id, 'event_id': event_id})).mappings().first(); print(json.dumps(dict(row), default=str))\nasyncio.run(main())"
```

Expected:

```json
{"event_id": "453a87cf-fa89-4b2c-92ac-0e1d5f3b837c", "conference_link": "https://meet.google.com/yqz-yaqx-dup", "online_meeting_provider": "googleMeet"}
```

- [ ] **Step 8: Commit**

Run:

```bash
git add scripts/maintenance/calendar/backfill_meeting_join_links.py tests/scripts/maintenance/calendar/test_backfill_meeting_join_links.py
git commit -m "chore: backfill calendar meeting join links"
```

## Task 5: Document the Calendar/Meeting Platform Contract

**Files:**
- Modify: `docs/Technical Documentation/integrations/sync-by-provider.md`

- [ ] **Step 1: Add documentation section**

Add this section to `docs/Technical Documentation/integrations/sync-by-provider.md`:

```markdown
## Calendar Provider vs Meeting Platform

Calendar provider and online meeting platform are separate concepts.

`calendar_events.provider` records where the calendar event was synced from, such as
`google`, `microsoft`, or `outlook`.

`calendar_events.conference_link` records the actual join URL that Kai/Recall can use,
regardless of which calendar provider delivered the event.

`calendar_events.online_meeting_provider` records the meeting platform inferred from the
join URL or structured provider conference metadata:

| Join URL host | online_meeting_provider |
| --- | --- |
| `meet.google.com` | `googleMeet` |
| `teams.microsoft.com`, `teams.live.com` | `teamsForBusiness` |
| `zoom.us`, `*.zoom.us` | `zoom` |
| `webex.com`, `*.webex.com` | `webex` |

Supported examples:

- Outlook calendar event with a Google Meet link in `location` or `locations`
- Outlook calendar event with a Zoom link in `locations`, `body`, or `bodyPreview`
- Outlook calendar event with native Teams `onlineMeeting.joinUrl`
- Outlook calendar event with a Teams/Webex link in body/location fields
- Google Calendar event with Google Meet `conferenceData`
- Google Calendar event with Zoom/Teams/Webex link in `conferenceData`, description, or location

Downstream meeting-bot logic must continue to use `conference_link` as the only
join target. It should not branch on calendar provider when deciding whether Kai can
join. Provider-specific parsing belongs in `CalendarEventNormalizer`.

Current-user meeting visibility uses `CalendarEventUserState` joined to the
provider-parent `KnowledgeIntegration` row (`google` or `microsoft`). Calendar
capability integration types (`google_calendar`, `outlook_calendar`) may remain
eligible in dispatch fallback checks for backward compatibility, but user-owned
query surfaces should not be widened to capability rows unless the user-state
writer is migrated to those rows.
```

- [ ] **Step 2: Commit**

Run:

```bash
git add "docs/Technical Documentation/integrations/sync-by-provider.md"
git commit -m "docs: define calendar meeting platform contract"
```

## Task 6: Focused Verification

**Files:**
- No file changes.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/service/integrations/calendar/test_normalizer.py tests/service/integrations/test_calendar_sync_service_incremental.py::TestCalendarIncrementalBatchWrites::test_incremental_sync_enqueues_outlook_event_when_join_link_is_newly_extracted tests/service/test_meetings_tab_service.py tests/service/crm/test_crm_meetings_access_service.py tests/agents/tools/intelligence/test_send_meeting_bot_invite.py tests/scripts/maintenance/calendar/test_backfill_meeting_join_links.py -v
```

Expected:

```text
PASSED
```

- [ ] **Step 2: Run existing meeting-bot dispatch tests**

Run:

```bash
uv run pytest tests/service/meeting_bot/test_meeting_recording_dispatch_service.py tests/service/integrations/test_calendar_sync_service_incremental.py tests/workers/deal_worker/services/test_deal_meeting_matcher_recording_dispatch.py -v
```

Expected:

```text
PASSED
```

- [ ] **Step 3: Run focused Ruff**

Run:

```bash
uv run ruff check src/service/integrations/calendar/normalizer.py src/service/integrations/calendar/integration_types.py src/service/meeting_bot/dispatch_service.py src/service/meetings_tab_service.py src/service/crm/access/meetings_access_service.py src/agents/tools/intelligence/send_meeting_bot_invite.py src/agents/tools/intelligence/list_meetings_unified.py scripts/maintenance/calendar/backfill_meeting_join_links.py tests/service/integrations/calendar/test_normalizer.py tests/service/integrations/test_calendar_sync_service_incremental.py tests/service/test_meetings_tab_service.py tests/service/crm/test_crm_meetings_access_service.py tests/agents/tools/intelligence/test_send_meeting_bot_invite.py tests/scripts/maintenance/calendar/test_backfill_meeting_join_links.py
```

Expected:

```text
All checks passed!
```

- [ ] **Step 4: Run focused MyPy if publishing**

Run only when preparing PR:

```bash
uv run mypy src/service/integrations/calendar/normalizer.py src/service/integrations/calendar/integration_types.py src/service/meeting_bot/dispatch_service.py src/service/meetings_tab_service.py src/service/crm/access/meetings_access_service.py src/agents/tools/intelligence/send_meeting_bot_invite.py src/agents/tools/intelligence/list_meetings_unified.py scripts/maintenance/calendar/backfill_meeting_join_links.py
```

Expected:

```text
Success: no issues found
```

## Rollout Notes

1. Ship code first so new calendar syncs populate `conference_link`.
2. Confirm the active target explicitly with `kubectl config current-context`; all runbook commands must still pass `--context`.
3. Run the backfill script in dev with dry-run for the confirmed Carlos event using `--context copilot-gtm-aks-dev`.
4. Apply the backfill script in dev for that event and verify the row.
5. Confirm Meetings tab shows:
   - Channel: Google Meet
   - Join Meeting button visible
   - Kai invite available or queued depending on recording state and dispatch window
6. Run a broader dev dry-run for the CopilotGTM org with `--days-ahead 30 --limit 500`.
7. Review JSON summaries for `matched`, `updated`, `queued`, `queue_failed`, `failed_event_ids`, and sample `conference_host` values before any apply run.
8. For prod, use the prod context explicitly in every command, run dry-run per affected org first, and review sample hosts before applying.
9. Apply prod backfill per org or per event after reviewing matched counts and sample rows.
10. If `queue_failed > 0`, retry only those `failed_event_ids` after confirming the rows were updated.

## Self-Review

Spec coverage:

- Outlook received Google Meet invite: Task 1, Task 2, Task 4.
- Outlook received Zoom, Webex, and Teams invite: Task 1, Task 2.
- Google Calendar received third-party meeting links: Task 1, Task 2.
- Structured Google add-on meeting links: Task 1, Task 2.
- Microsoft Safe Links that wrap supported meeting URLs: Task 1, Task 2.
- Manual Invite Kai and backend dispatch: covered by preserving `conference_link`, centralizing integration type contracts, the new incremental sync enqueue regression in Task 2, and running meeting-bot tests in Task 6.
- Existing skipped rows: Task 4.
- Production-safe backfill queueing after commit and after session release: Task 4.
- Backfill queue failure visibility and retryability: Task 4.
- Kubernetes-context explicit runbook commands: Task 4 and Rollout Notes.
- JSON backfill output for operator review: Task 4.
- Documentation: Task 5.

Placeholder scan:

- The plan contains concrete file paths, commands, code snippets, and expected outputs.

Type consistency:

- `conference_link` and `online_meeting_provider` match `CalendarEvent` fields.
- `CalendarEventNormalizer.extract_join_link_update()` is public and reused by the backfill script.
- Current-user surfaces use `PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES`.
- Dispatch uses `DISPATCH_CALENDAR_INTEGRATION_TYPES`.
