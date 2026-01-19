# Task: Fix Timeline Provider Mapping

## Status: REVIEW

## Problem Statement
Currently, when timeline events (Meetings and Emails) are created, the provider source is often incorrectly mapped. Specifically, Outlook meetings are being shown as Google meetings because the creation methods default to Google/Gmail sources.

## Proposed Solution
1. Update timeline creation methods to correctly identify the provider from the underlying integration or event data.
2. Ensure `TimelineEventSource` is correctly set based on whether the event came from Google, Outlook, or Teams.

## Related Files & Folders
- `src/service/crm/deal_timeline_service.py`: `create_meeting_timeline_event` and `create_email_timeline_event` no longer default to Google/Gmail when provider info is available.
- `src/service/crm/deal_timeline_sync_service.py`: `build_meeting_timeline_event` and `build_email_timeline_event` now accept an inferred source; sync passes the correct provider.
- `src/workers/deal_worker/services/deal_meeting_matcher.py`: Resolves meeting provider before building timeline event.
- `src/workers/deal_worker/services/deal_email_matcher.py`: Resolves email provider before building timeline event.
- `src/domain/models/organisation_tables/sync/timeline_models.py`: Defines `TimelineEventSource` enum.
