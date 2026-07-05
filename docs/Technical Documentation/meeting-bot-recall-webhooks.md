# Recall meeting bot webhook contract

This document is the backend contract for Recall webhooks handled by
`src/service/meeting_bot/webhook_service.py`.

## Webhook endpoint

Configure each Recall environment to send selected events to:

```text
/users/meeting-bot/recall/webhook
```

The HTTP router verifies the Recall signature and enqueues worker processing.
The worker finds the org-scoped `meeting_recording_sessions` row by Recall bot id.

## Events we capture

| Recall event | Local effect |
| --- | --- |
| `bot.joining_call` | `recording_status=JOINING` |
| `bot.in_waiting_room` | `recording_status=JOINING` |
| `bot.in_call_not_recording` | `recording_status=IN_CALL` |
| `bot.recording_permission_allowed` | `recording_status=IN_CALL` |
| `bot.recording_permission_denied` | `recording_status=FAILED`, `transcript_status=FAILED` |
| `bot.in_call_recording` | `recording_status=IN_CALL`, records first recording timestamp |
| `bot.call_ended` | `COMPLETED` only if recording had started; otherwise `FAILED` |
| `bot.done` | `COMPLETED` only if recording had started; preserves pre-join failure |
| `bot.fatal` | `recording_status=FAILED`, `transcript_status=FAILED` |
| `recording.done` | `recording_status=COMPLETED` |
| `recording.failed` | `recording_status=FAILED`, `transcript_status=FAILED` |
| `recording.deleted` | `recording_status=CANCELLED`, `transcript_status=NOT_AVAILABLE` |
| `transcript.processing` | `transcript_status=PENDING` |
| `transcript.done` | Fetches Recall bot snapshot, stores transcript artifact, materializes transcript, then creates meeting summary jobs |
| `transcript.failed` | `transcript_status=FAILED` |
| `transcript.deleted` | `transcript_status=NOT_AVAILABLE` |
| `video_mixed.done` | Fetches Recall bot snapshot, downloads the mixed MP4, stores it in org blob storage, and records the archived blob path |

## Events to subscribe in Recall dashboard

For dev first, enable only these events on the dev endpoint:

```text
bot.joining_call
bot.in_waiting_room
bot.in_call_not_recording
bot.recording_permission_allowed
bot.recording_permission_denied
bot.in_call_recording
bot.call_ended
bot.done
bot.fatal
recording.done
recording.failed
recording.deleted
transcript.processing
transcript.done
transcript.failed
transcript.deleted
video_mixed.done
```

After dev deploy and one end-to-end bot run verifies webhook delivery, mirror the
same event list to prod.

## Stored video artifact

`video_mixed.done` stores the Recall artifact under
`meeting_recording_sessions.provider_payload.video_artifact`.

Expected fields:

```json
{
  "recording_id": "recall-recording-id",
  "video_id": "recall-video-id",
  "download_url": "recall-signed-url",
  "archived_blob_path": "meeting_recordings/{event_id}/{recording_id}/{video_id}.mp4",
  "archived_content_type": "video/mp4",
  "archived_size_bytes": 123456,
  "archived_at": "2026-07-05T13:45:00+00:00"
}
```

`download_url` is kept for provider debugging only. Playback should prefer
`archived_blob_path`; the Meetings API turns that path into a short-lived read
SAS when a user opens the recording.

## Events we intentionally do not subscribe to now

| Event family | Reason |
| --- | --- |
| `video_mixed.processing`, `video_mixed.failed`, `video_mixed.deleted` | V1 only needs the final downloadable mixed MP4. Recording/transcript failures already release the UI from processing. |
| `video_separate.*` | We do not expose per-participant video. |
| `audio_mixed.*`, `audio_separate.*` | We do not expose standalone audio artifacts. |
| `participant_events.*` | No UI or summary pipeline consumes participant event artifacts yet. |
| `meeting_metadata.*` | Calendar and meeting metadata are already owned by our calendar sync tables. |
| `bot.breakout_room_*` | No current workflow needs breakout room timeline state. |

## User-facing status guidance

Frontend should not infer bot failure from `transcript_status` alone.

Use:

- `bot.status=recording_live` for `JOINING` or `IN_CALL`
- `bot.status=already_sent` for provider-accepted scheduled bots
- `bot.status=failed` plus `bot.reason` for provider failure reasons such as bot could not join, host denied recording, or bot was removed before recording
- `recording_availability=Available` only when `recording_status=COMPLETED`

`transcript_status=FAILED` means transcript output is not available. It does not
by itself explain whether the bot failed to join, was denied permission, was
removed, or had a transcription provider error.
