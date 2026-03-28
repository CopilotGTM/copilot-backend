# Chat System Test Plan

## Why Existing Tests Don't Catch Production Bugs

### Problem: Tests Verify Fakes, Not Real Behavior

The existing test suite uses `FakeChatMessageService` and `FakeChatSessionService` that
**never reject requests**. The real services have security checks, DB constraints, and
error handling that the fakes silently skip.

**Example**: `FakeChatMessageService.create_user_message()` calls `_get_or_create_session()`
which auto-creates sessions. The real `ChatMessageService.create_user_message()` does a DB
query with `organisation_id + user_id + is_deleted=False` and raises `ValueError("Chat session
not found")`. Result: tests pass for any session_id, but production rejects invalid sessions.

### Solution: Test Real Service Logic, Mock Only External I/O

Each test category below specifies what to mock (external I/O only) and what to test with
real logic. The principle: **mock the database session and storage, but use the real service
class with real validation logic.**

---

## Test Architecture

### Layers to Test

```
Layer 1: Service Logic (ChatMessageService, ChatSessionService, UploadService)
  → Real service classes + mocked AsyncSession
  → Tests: validation, business rules, SQL query construction

Layer 2: Worker Processing (MessageProcessingService, BotMessageProcessingService, PostUploadService)
  → Real service classes + mocked DB sessions, Redis, Agent
  → Tests: 3-phase flow, error handling, frame processing, job state transitions

Layer 3: Agent Tools (chat_attachments, fetch_teams_chat_messages)
  → Real tool functions + mocked AgentContext, storage, DB
  → Tests: access control, content extraction, error paths

Layer 4: API Handlers (messages_handler, sessions_handler, upload_handler)
  → FastAPI TestClient + real validation, mocked services
  → Tests: request validation, response format, error responses

Layer 5: SSE Streaming (ChatMessageSSEService, AttachmentSSEService)
  → Real service classes + mocked Redis
  → Tests: stream lifecycle, consumer groups, heartbeat, timeout
```

### Mock Strategy

| Dependency | Mock Approach | Why |
|---|---|---|
| AsyncSession / Session | `AsyncMock` with configurable `execute()` results | Test real query-building logic, control what DB "returns" |
| AzureOrganisationBlobStorage | Fake with in-memory dict | Test storage interaction logic without Azure |
| Redis (Streams/Pubsub) | `fakeredis` or in-memory mock | Test publish/consume logic |
| Celery tasks | `mocker.patch()` | Prevent actual task dispatch |
| Agent (GeneralAgent) | Mock `run_stream()` yielding controlled frames | Test frame processing without LLM calls |
| `acquire_org_session` / `acquire_org_async_session` | Yield mock session | Control DB behavior per test |

---

## Test Plan by Component

### 1. ChatSessionService Tests

**File**: `tests/service/chat/test_chat_session_service.py`

**Existing coverage**: Only tested via API handler with FakeChatSessionService (no real service tests)

#### 1.1 Session Creation
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 1 | `test_create_session_stores_with_correct_org_and_user` | Session created with constructor's org_id and user_id | HIGH |
| 2 | `test_create_session_normalizes_string_chat_type` | String "GENERAL_CHAT" → ChatType.GENERAL_CHAT enum | HIGH |
| 3 | `test_create_session_invalid_chat_type_defaults_to_general` | Unknown chat_type string → GENERAL_CHAT | MEDIUM |
| 4 | `test_create_session_empty_title_raises_value_error` | "", "  ", "\n\t" → ValueError | HIGH |
| 5 | `test_create_session_truncates_title_at_100_chars` | 101+ chars → first 97 + "..." | HIGH |
| 6 | `test_create_session_preserves_title_at_exactly_100_chars` | Exactly 100 chars → unchanged | MEDIUM |
| 7 | `test_create_session_stores_tags_and_properties` | Tags/properties persisted correctly | MEDIUM |
| 8 | `test_create_session_commits_and_refreshes` | db.commit() and db.refresh() called | HIGH |

#### 1.2 Session Deletion
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 9 | `test_delete_session_sets_is_deleted_true` | Soft delete via is_deleted flag | HIGH |
| 10 | `test_delete_session_not_found_raises_value_error` | Missing session → ValueError | HIGH |
| 11 | `test_delete_session_already_deleted_raises_value_error` | is_deleted=True → ValueError | HIGH |
| 12 | `test_delete_session_wrong_user_raises_value_error` | Session owned by other user → ValueError | HIGH |
| 13 | `test_delete_session_wrong_org_raises_value_error` | Session in other org → ValueError | HIGH |

#### 1.3 Session Rename
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 14 | `test_rename_session_updates_title` | Title updated and committed | HIGH |
| 15 | `test_rename_session_truncates_long_title` | Same 100-char truncation logic | MEDIUM |
| 16 | `test_rename_session_empty_title_raises_value_error` | Empty title rejected | HIGH |
| 17 | `test_rename_session_not_found_raises_value_error` | Missing/deleted session → ValueError | HIGH |

#### 1.4 Session Listing
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 18 | `test_list_sessions_filters_by_org_and_user` | Only caller's sessions returned | HIGH |
| 19 | `test_list_sessions_excludes_deleted` | is_deleted=True sessions excluded | HIGH |
| 20 | `test_list_sessions_excludes_teams_chat_type` | TEAMS_CHAT sessions excluded | HIGH |
| 21 | `test_list_sessions_cursor_pagination_keyset_ordering` | Correct (created_at, session_id) keyset | HIGH |
| 22 | `test_list_sessions_returns_none_cursor_on_last_page` | No more items → cursor=None | MEDIUM |

#### 1.5 Session Search
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 23 | `test_search_sessions_ilike_match` | Case-insensitive partial match | HIGH |
| 24 | `test_search_sessions_empty_query_raises_value_error` | Empty query rejected | MEDIUM |
| 25 | `test_search_sessions_query_exceeds_50_chars_raises` | Long query rejected | MEDIUM |
| 26 | `test_search_sessions_excludes_deleted_and_teams` | Filters applied during search | HIGH |

#### 1.6 Favorites
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 27 | `test_set_favorite_true_adds_to_properties` | properties["favorite"] = True | HIGH |
| 28 | `test_set_favorite_false_removes_from_properties` | "favorite" key removed | HIGH |
| 29 | `test_set_favorite_not_found_raises` | Missing session → ValueError | MEDIUM |

#### 1.7 Retry Logic
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 30 | `test_execute_with_retry_retries_on_deque_mutated_error` | RuntimeError("deque mutated...") → retry | HIGH |
| 31 | `test_execute_with_retry_raises_on_other_runtime_errors` | Other RuntimeErrors not retried | HIGH |
| 32 | `test_execute_with_retry_gives_up_after_max_retries` | Retry limit respected | MEDIUM |

---

### 2. ChatMessageService Tests

**File**: `tests/service/chat/test_chat_message_service.py`

**Existing coverage**: Only tested via API handler with FakeChatMessageService (no real service tests)

#### 2.1 Message Creation
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 1 | `test_create_user_message_validates_session_ownership` | Session must match org_id + user_id + not deleted | CRITICAL |
| 2 | `test_create_user_message_missing_session_raises_value_error` | No matching session → ValueError | CRITICAL |
| 3 | `test_create_user_message_deleted_session_raises_value_error` | Deleted session → ValueError | HIGH |
| 4 | `test_create_user_message_wraps_content_as_json` | content stored as {"text": content} | HIGH |
| 5 | `test_create_user_message_creates_pending_job` | ChatMessageJob created with status=PENDING | HIGH |
| 6 | `test_create_user_message_job_metadata_contains_all_ids` | session_id, user_message_id, assistant_message_id in metadata | HIGH |
| 7 | `test_create_user_message_returns_tuple_of_message_job_assistant_id` | Correct return type and values | HIGH |
| 8 | `test_create_user_message_flushes_before_linking_attachments` | db.flush() called to get message_id | HIGH |

#### 2.2 Attachment Resolution
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 9 | `test_resolve_attachments_filters_by_org_user_not_deleted` | Only caller's non-deleted attachments | CRITICAL |
| 10 | `test_resolve_attachments_empty_ids_returns_empty_list` | None/empty → [] | MEDIUM |
| 11 | `test_resolve_attachments_nonexistent_ids_returns_empty` | IDs not in DB → empty list (no error) | HIGH |
| 12 | `test_create_link_sets_correct_order` | attachment_order matches enumerate index | MEDIUM |

#### 2.3 Message Listing
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 13 | `test_list_messages_validates_session_ownership` | Session must match org+user+not deleted | CRITICAL |
| 14 | `test_list_messages_orders_by_created_at_desc_message_id_desc` | Keyset ordering correct | HIGH |
| 15 | `test_list_messages_cursor_decodes_and_filters_correctly` | Cursor-based keyset pagination | HIGH |
| 16 | `test_list_messages_filters_by_role` | Role filter applied | MEDIUM |
| 17 | `test_list_messages_filters_by_message_type` | Message type filter applied | MEDIUM |
| 18 | `test_list_messages_limit_plus_one_for_has_more_detection` | Fetches limit+1 rows | MEDIUM |

#### 2.4 Attachment URLs & List Items
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 19 | `test_get_message_attachments_joins_link_table_correctly` | M2M join with correct ordering | HIGH |
| 20 | `test_get_message_attachments_filters_by_org_user_not_deleted` | Security filters on attachment query | CRITICAL |
| 21 | `test_build_attachment_urls_generates_sas_for_thumbnails` | SAS URL generated for thumbnail_small_path | HIGH |
| 22 | `test_build_attachment_urls_handles_missing_thumbnail_path` | None path → None URL | MEDIUM |
| 23 | `test_build_attachment_urls_handles_sas_generation_failure` | Exception → None URL (not crash) | HIGH |
| 24 | `test_build_message_list_items_extracts_text_from_content_dict` | {"text": "..."} → "..." | HIGH |
| 25 | `test_build_message_list_items_converts_confidence_score` | Numeric → categorical string | MEDIUM |

#### 2.5 Feedback
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 26 | `test_update_feedback_verifies_message_ownership_via_session_join` | Join on ChatSession for ownership | CRITICAL |
| 27 | `test_update_feedback_score_zero_clears_to_none` | 0 → None | HIGH |
| 28 | `test_update_feedback_invalid_score_raises_value_error` | Scores outside {-1,0,1} → ValueError | HIGH |
| 29 | `test_update_feedback_nonexistent_message_raises_value_error` | Missing message → ValueError | HIGH |

#### 2.6 Confidence Score Conversion
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 30 | `test_to_confidence_string_labels_returned_as_is` | "high" → "high", "very_low" → "very_low" | MEDIUM |
| 31 | `test_to_confidence_numeric_thresholds` | 0.8+ → "high", 0.5-0.79 → "medium", <0.5 → "low" | MEDIUM |
| 32 | `test_to_confidence_none_returns_none` | None → None | LOW |
| 33 | `test_to_confidence_unknown_string_returned_stripped` | "unknown_value" → "unknown_value" | LOW |

---

### 3. MessageProcessingService Tests (Worker)

**File**: `tests/workers/chat_worker/services/test_message_processing_service.py`

**Existing coverage**: Only frame processing and attachment resolution tested. The core `process()` flow is untested.

#### 3.1 Job Loading (_load_job_data)
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 1 | `test_load_job_data_marks_job_in_progress` | Job status → IN_PROGRESS on load | CRITICAL |
| 2 | `test_load_job_data_retries_when_job_not_found_with_celery_task` | Celery retry with exponential backoff | HIGH |
| 3 | `test_load_job_data_returns_none_when_job_not_found_without_celery` | No celery_task → return None | HIGH |
| 4 | `test_load_job_data_raises_on_missing_metadata_ids` | Missing session_id/user_message_id/assistant_message_id → RuntimeError | HIGH |
| 5 | `test_load_job_data_raises_on_invalid_uuid_in_metadata` | Non-UUID strings → RuntimeError | HIGH |
| 6 | `test_load_job_data_raises_when_session_not_found` | Missing ChatSession → RuntimeError | HIGH |
| 7 | `test_load_job_data_raises_when_user_message_not_found` | Missing ChatMessage → RuntimeError | HIGH |
| 8 | `test_load_job_data_loads_history_ordered_by_created_at_asc` | History chronologically ordered | HIGH |
| 9 | `test_load_job_data_limits_history_to_30_messages` | Max 30 messages loaded | MEDIUM |
| 10 | `test_load_job_data_builds_attachment_manifest_per_message` | Attachment manifests built from link table | HIGH |
| 11 | `test_load_job_data_skips_pending_upload_and_failed_attachments` | Status filter on attachments | HIGH |
| 12 | `test_load_job_data_looks_up_user_email_from_master_schema` | Cross-schema user email lookup | MEDIUM |

#### 3.2 Agent Workflow Execution (_execute_agent_workflow)
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 13 | `test_execute_agent_workflow_returns_agent_result_from_stream` | Frames accumulated into AgentResult | HIGH |
| 14 | `test_execute_agent_workflow_returns_error_result_on_exception` | Exception → error apology message | CRITICAL |
| 15 | `test_execute_agent_workflow_returns_error_result_on_none_stream` | None stream → error result | HIGH |

#### 3.3 Result Saving (_save_results)
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 16 | `test_save_results_creates_assistant_message_with_correct_fields` | All fields populated (content, agent_data, extra, model_name) | CRITICAL |
| 17 | `test_save_results_sets_parent_message_to_user_message` | Threading: parent_message_id set | HIGH |
| 18 | `test_save_results_finalizes_job_with_completed_status` | Job → COMPLETED with result_data | CRITICAL |
| 19 | `test_save_results_persists_token_usage_and_cost` | token_usage, cost (Decimal), latency_ms saved | HIGH |
| 20 | `test_save_results_warns_when_no_token_usage` | Logger warning when token_usage is None | MEDIUM |
| 21 | `test_save_results_publishes_final_frames_to_redis` | answer.replace, suggestions.replace, finalize frames published | HIGH |
| 22 | `test_save_results_schedules_stream_cleanup` | Background cleanup thread started | MEDIUM |

#### 3.4 Frame Processing (_process_frame_data)
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 23 | `test_process_frame_answer_delta_accumulates_parts` | Multiple deltas concatenated | HIGH |
| 24 | `test_process_frame_answer_replace_overwrites` | Replace sets final_answer | HIGH |
| 25 | `test_process_frame_suggestions_extracts_labels` | String and dict items handled | MEDIUM |
| 26 | `test_process_frame_sources_replace_stored` | Sources stored directly | MEDIUM |
| 27 | `test_process_frame_visualization_appended` | Visualization data captured | MEDIUM |
| 28 | `test_process_frame_structured_data_appended` | Structured data captured | MEDIUM |
| 29 | `test_process_frame_task_list_filters_non_dicts` | Non-dict task items skipped | MEDIUM |
| 30 | `test_process_frame_token_usage_captures_all_fields` | All 6 token fields + model + cost + duration | HIGH |

#### 3.5 Error Handling & Job Failure
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 31 | `test_process_marks_job_failed_on_unhandled_exception` | Exception → _mark_job_failed called | CRITICAL |
| 32 | `test_mark_job_failed_truncates_error_to_500_chars` | Long error messages truncated | MEDIUM |
| 33 | `test_mark_job_failed_silently_handles_db_failure` | DB error during failure marking → logged, not re-raised | HIGH |
| 34 | `test_process_returns_false_on_failure` | Failed processing → return False | HIGH |

#### 3.6 Full Process Flow (Integration)
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 35 | `test_process_happy_path_load_execute_save` | Phase 1→2→3 completes successfully | CRITICAL |
| 36 | `test_process_load_failure_skips_execution_and_save` | load returns None → returns False | HIGH |
| 37 | `test_process_agent_failure_still_saves_error_result` | Agent error → error result saved | HIGH |

---

### 4. BotMessageProcessingService Tests

**File**: `tests/workers/chat_worker/services/test_bot_message_processing_service.py`

**Existing coverage**: Message building and job tracking tested. Full process flow untested.

#### 4.1 Process Flow
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 1 | `test_process_teams_personal_chat_persists_messages` | User + assistant messages saved to DB | CRITICAL |
| 2 | `test_process_teams_group_chat_does_not_persist` | Group messages NOT saved to DB | HIGH |
| 3 | `test_process_slack_channel_does_not_persist` | Channel messages NOT saved to DB | HIGH |
| 4 | `test_process_sends_reply_via_platform_adapter` | Reply sent to correct platform | HIGH |
| 5 | `test_process_sends_error_message_when_agent_fails` | Static error message sent on failure | CRITICAL |
| 6 | `test_process_marks_job_completed_with_token_usage` | Job finalized after success | HIGH |
| 7 | `test_process_marks_job_failed_on_error` | Job marked FAILED on exception | HIGH |

#### 4.2 Audio Transcription
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 8 | `test_process_transcribes_slack_voice_notes` | Audio files transcribed before agent call | HIGH |
| 9 | `test_merge_audio_transcript_audio_only` | Transcript used as message when no typed text | HIGH |
| 10 | `test_merge_audio_transcript_with_typed_text` | Typed text + transcript combined | MEDIUM |

#### 4.3 Attachment Ingestion
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 11 | `test_process_ingests_slack_attachments` | Slack files downloaded and normalized | HIGH |
| 12 | `test_process_ingests_teams_attachments` | Teams files downloaded and normalized | HIGH |
| 13 | `test_ingest_respects_max_file_size_limit` | BOT_ATTACHMENT_MAX_INGEST_BYTES enforced | MEDIUM |

---

### 5. UploadService Tests (Chat Attachments)

**File**: `tests/service/chat/test_upload_service.py`

**Existing coverage**: Only tested via API handler with FakeUploadService

#### 5.1 Presign Upload
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 1 | `test_presign_creates_pending_upload_attachment` | Attachment record with status=PENDING_UPLOAD | HIGH |
| 2 | `test_presign_generates_write_sas_url` | SAS URL with write permission, 15min expiry | HIGH |
| 3 | `test_presign_sets_correct_storage_path` | Path includes org, user, attachment_id | HIGH |
| 4 | `test_presign_stores_client_reported_size_in_metadata` | attachment_metadata populated | MEDIUM |

#### 5.2 Finalize Upload
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 5 | `test_finalize_verifies_blob_exists` | Checks blob in storage before finalizing | CRITICAL |
| 6 | `test_finalize_transitions_to_processing` | Status: PENDING_UPLOAD → PROCESSING | HIGH |
| 7 | `test_finalize_nonexistent_blob_raises` | Missing blob → error | HIGH |

#### 5.3 Delete Attachment
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 8 | `test_delete_rejects_linked_attachment` | Attachment linked to message → cannot delete | CRITICAL |
| 9 | `test_delete_soft_deletes_and_removes_blobs` | is_deleted=True + blob deletion | HIGH |
| 10 | `test_delete_wrong_user_rejects` | Other user's attachment → reject | CRITICAL |

#### 5.4 Batch Operations
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 11 | `test_presign_batch_creates_multiple_attachments` | All items created with errors tracked | MEDIUM |
| 12 | `test_finalize_batch_processes_all_and_reports_errors` | Per-item error reporting | MEDIUM |

---

### 6. PostUploadService Tests

**File**: `tests/workers/chat_worker/services/test_post_upload_service.py`

**Existing coverage**: None

#### 6.1 Processing Pipeline
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 1 | `test_process_computes_sha256_checksum` | Checksum calculated from blob content | HIGH |
| 2 | `test_process_generates_small_and_medium_thumbnails` | 128px and 512px thumbnails created | HIGH |
| 3 | `test_process_sets_status_completed` | Status → COMPLETED on success | CRITICAL |
| 4 | `test_process_sets_status_failed_on_error` | Status → FAILED on exception | CRITICAL |
| 5 | `test_process_skips_already_completed_attachments` | COMPLETED status → skip | HIGH |
| 6 | `test_process_publishes_status_to_redis` | Redis pubsub notification sent | HIGH |

---

### 7. ChatMessageSSEService Tests

**File**: `tests/service/chat/test_chat_message_sse_service.py`

**Existing coverage**: None

#### 7.1 Stream Lifecycle
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 1 | `test_stream_yields_ready_event_first` | "ready" event is first SSE event | HIGH |
| 2 | `test_stream_yields_frames_from_redis_stream` | Frames read from Redis consumer group | CRITICAL |
| 3 | `test_stream_yields_ping_on_heartbeat_interval` | Ping every 25s when no data | HIGH |
| 4 | `test_stream_yields_complete_when_job_done` | "complete" event on finalize frame | CRITICAL |
| 5 | `test_stream_yields_timeout_after_max_duration` | "timeout" event after 900s | MEDIUM |
| 6 | `test_stream_supports_resume_from_last_event_id` | Last-Event-ID → resume from that point | HIGH |
| 7 | `test_stream_consumer_group_consume_once` | Each frame delivered exactly once | CRITICAL |

---

### 8. ExternalChatAttachmentIngestService Tests

**File**: `tests/service/chat/test_external_chat_attachment_ingest_service.py`

**Existing coverage**: Static methods only (resolve_teams_download, guess_mimetype). No ingest flow tests.

#### 8.1 Slack Ingestion
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 1 | `test_ingest_slack_files_downloads_with_bot_token` | Bot token in Authorization header | HIGH |
| 2 | `test_ingest_slack_files_respects_max_bytes_limit` | Files over limit skipped | HIGH |
| 3 | `test_ingest_slack_files_creates_attachment_records` | Attachment + blob stored | HIGH |
| 4 | `test_ingest_slack_files_enqueues_post_upload_task` | Celery task dispatched | MEDIUM |

#### 8.2 Teams Ingestion
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 5 | `test_ingest_teams_inline_uses_bot_token` | Inline source → bot token auth | HIGH |
| 6 | `test_ingest_teams_onedrive_uses_graph_token` | OneDrive → Graph API token | HIGH |
| 7 | `test_ingest_teams_guesses_mimetype_from_extension` | Fallback MIME type detection | MEDIUM |

---

### 9. Agent Tool Tests (chat_attachments)

**File**: `tests/agents/tools/communication/test_chat_attachments.py`

**Existing coverage**: Good coverage of read/list. Missing security boundary tests.

#### 9.1 Access Control (Security)
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 1 | `test_read_attachment_from_other_session_denied` | Attachment in different session → error | CRITICAL |
| 2 | `test_read_attachment_from_other_org_denied` | Cross-org attachment → error | CRITICAL |
| 3 | `test_list_attachments_only_returns_current_session` | No leakage from other sessions | CRITICAL |

#### 9.2 Content Extraction Edge Cases
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 4 | `test_read_pptx_extracts_slides_with_tables_and_notes` | PPTX extraction including tables, groups, notes | HIGH |
| 5 | `test_read_xlsx_extracts_multiple_sheets` | Multi-sheet XLSX extraction | HIGH |
| 6 | `test_read_empty_pdf_returns_empty_string` | Empty PDF → "" not crash | MEDIUM |
| 7 | `test_read_large_text_truncated_with_indicator` | Truncation flag set correctly | MEDIUM |

---

### 10. Cross-Cutting Concerns

#### 10.1 Organization Isolation (Security)
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 1 | `test_user_cannot_access_other_users_sessions` | User A → User B's session → ValueError | CRITICAL |
| 2 | `test_user_cannot_access_other_orgs_sessions` | Org A → Org B's session → ValueError | CRITICAL |
| 3 | `test_user_cannot_list_other_users_messages` | User A → User B's messages → empty/error | CRITICAL |
| 4 | `test_user_cannot_access_other_users_attachments` | Cross-user attachment → empty list | CRITICAL |
| 5 | `test_worker_processes_only_own_org_attachments` | Attachment query filters by org_id | CRITICAL |

#### 10.2 Database Session Discipline
| # | Test | Behavior Verified | Priority |
|---|---|---|---|
| 6 | `test_message_processing_releases_session_before_agent_call` | No session held during Phase 2 | CRITICAL |
| 7 | `test_message_processing_acquires_fresh_session_for_save` | New session for Phase 3 | HIGH |
| 8 | `test_bot_processing_releases_session_before_agent_call` | Same pattern for bot service | HIGH |

---

## Execution Priority

### Phase 1: Security & Data Isolation (CRITICAL)
Tests 10.1.1-5, 2.1.1-3, 2.3.1, 2.5.1, 5.3.2, 9.1.1-3

These tests verify the most dangerous class of bugs: data leaking between users/orgs.

### Phase 2: Core Business Logic
Tests 1.1-1.7, 2.1-2.5, 3.1-3.5, 5.1-5.3

These tests verify the service layer works correctly with real validation logic.

### Phase 3: Worker Processing Pipeline
Tests 3.1-3.6, 4.1-4.3, 6.1

These tests verify the async processing pipeline that runs in Celery workers.

### Phase 4: SSE Streaming & Real-time
Tests 7.1

These tests verify the real-time streaming that the frontend depends on.

### Phase 5: Edge Cases & Robustness
Remaining MEDIUM/LOW priority tests.

---

## Implementation Notes

### Pattern for Testing Real Services with Mock DB

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.service.chat.chat_session_service import ChatSessionService


@pytest.fixture
def mock_session():
    """Create a mock AsyncSession that behaves like a real one."""
    session = AsyncMock()
    return session


@pytest.fixture
def service(mock_session):
    org_id = uuid4()
    user_id = uuid4()
    return ChatSessionService(db=mock_session, organisation_id=org_id, user_id=user_id)


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_empty_title_raises(self, service):
        with pytest.raises(ValueError, match="Title cannot be null or empty"):
            await service.create_session(title="")

    @pytest.mark.asyncio
    async def test_truncates_long_title(self, service, mock_session):
        # Mock the DB operations
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()
        mock_session.add = MagicMock()

        long_title = "x" * 150
        await service.create_session(title=long_title)

        # Verify what was added to the DB
        added_obj = mock_session.add.call_args[0][0]
        assert len(added_obj.title) <= 100
        assert added_obj.title.endswith("...")
```

### Pattern for Testing Worker Services with Mock DB Sessions

```python
import pytest
from unittest.mock import patch, MagicMock
from uuid import uuid4

from src.workers.chat_worker.services.message_processing_service import (
    MessageProcessingService,
)


class TestMessageProcessingService:
    @pytest.fixture
    def org_id(self):
        return uuid4()

    @pytest.fixture
    def service(self, org_id):
        return MessageProcessingService(organisation_id=org_id)

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_load_job_not_found_returns_none(self, mock_acquire, service):
        mock_session = MagicMock()
        mock_session.get.return_value = None  # Job not found
        mock_acquire.return_value = iter([mock_session])

        result = service._load_job_data(uuid4(), celery_task=None)
        assert result is None
```
