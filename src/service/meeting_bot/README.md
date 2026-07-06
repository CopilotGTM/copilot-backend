# Meeting Bot Services

`MeetingRecordingLifecycleService` owns scheduling, rescheduling, and cancellation.
`MeetingRecordingSessionService` owns `meeting_recording_sessions` writes.
`RecallMeetingRecordingWebhookService` maps provider callbacks to local state.
`MeetingSummaryTriggerService` handles transcript-ready summary jobs.
`MeetingRecordingArtifactService` archives provider media.
Provider-specific HTTP code lives under `providers/`.

Calendar sync, meetings APIs, deal matching, and agent tools should not call Recall
directly. They enqueue or delegate to the lifecycle service.
