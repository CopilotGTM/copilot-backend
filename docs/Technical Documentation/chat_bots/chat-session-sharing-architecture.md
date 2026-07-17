# Read-Only Chat Session Sharing Architecture

**Status:** Implemented
**Scope:** Authenticated users within one CopilotGTM organisation
**Decision:** Share the original chat session with specifically selected users through a separate access table. The owner remains the only writer.

## 1. Problem

Sales users need to share selected chats with colleagues or managers, especially chats containing audio messages. A recipient must see the existing history and any messages added later, but must not send messages or mutate the chat.

The feature must:

- share a specific chat with specific users in the same organisation;
- keep the original chat as the single source of truth;
- expose future messages and attachments to authorized viewers;
- keep all chat mutations owner-only;
- support revocation and a "Shared with me" view;
- preserve tenant isolation and attachment security; and
- remain simple enough to implement and operate without a collaboration subsystem.

## 2. Decision

Add one relational access table, `chat_session_access`, representing which users may read a chat session.

Do not:

- clone the chat;
- copy messages into another user's session;
- store viewer IDs in `ChatSession.properties` or a JSON/array column;
- add numbered viewer columns to `chat_sessions`;
- add editor roles, group sharing, public links, or organisation-wide visibility in V1; or
- reuse the owner-facing message-job stream as a multi-viewer stream.

The authorization invariant is:

```text
READ  = caller owns the session OR an access row exists for the caller
WRITE = caller owns the session
```

All viewers are read-only. There is no permission enum in V1 because the product has only one shared permission.

## 3. Why A Separate Table

Specific-user sharing is a many-to-many relationship:

```text
one chat session -> many viewers
one viewer       -> many shared chat sessions
```

A separate table is the normal relational representation of that relationship. It provides database uniqueness, efficient "Shared with me" queries, straightforward revocation, and a clean place for grant metadata.

Adding sharing columns to `chat_sessions` works only for single-value properties such as the owner. A JSON or UUID array would make membership validation, indexing, concurrent updates, and revocation harder without reducing the number of authorization rules the application must enforce.

Cloning is not sharing. A clone is a snapshot and would stop receiving new messages unless a synchronization system were added. Synchronization would introduce duplicate state, conflict handling, partial failures, and unclear deletion semantics for no product benefit.

## 4. Data Model

### 4.1 Existing owner

`ChatSession.user_id` remains the owner. Ownership is not transferred or duplicated.

### 4.2 New table

```text
chat_session_access
-------------------------------------------------------
organisation_id      UUID        NOT NULL
session_id           UUID        NOT NULL
viewer_user_id       UUID        NOT NULL
granted_by_user_id   UUID        NOT NULL
created_at           TIMESTAMPTZ NOT NULL

PRIMARY KEY (session_id, viewer_user_id)
FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id)
    ON DELETE CASCADE
INDEX (organisation_id, viewer_user_id, created_at DESC)
```

`viewer_user_id` and `granted_by_user_id` follow the existing chat-table pattern of storing application user identifiers. Before a grant is written, the application must verify against the user source of truth that:

1. the target user exists and is active;
2. the target user belongs to the same organisation as the session;
3. the target user is not the owner; and
4. the caller owns the session.

Revocation deletes the access row. A separate permission column, soft-delete flag, expiry time, group principal, or share-link token is not required for V1. These can be added through migrations if a validated requirement appears later.

## 5. Authorization Boundary

The current chat services scope session reads and writes directly to `ChatSession.user_id == caller_user_id`. Sharing must replace scattered owner checks with two reusable query helpers:

```text
resolve_readable_chat_session(...)
    owner OR matching chat_session_access row

resolve_owned_chat_session(...)
    owner only
```

The helpers must always include:

- `organisation_id == caller.organisation_id`;
- `is_deleted == false`; and
- the required owner/access predicate.

Use `resolve_readable_chat_session` for:

- opening a session;
- listing messages;
- listing message attachments;
- generating audio playback or thumbnail URLs; and
- checking for newly committed messages.

Use `resolve_owned_chat_session` for:

- sending messages;
- persisting realtime voice turns;
- renaming, favoriting, or deleting a session;
- changing feedback;
- granting or revoking access; and
- subscribing to the owner's processing-job events.

Returning `404 Not Found` for an inaccessible session is preferred over revealing that another user's private session exists.

## 6. API Contract

The final route names should follow the existing chat router prefix. The resource behavior is:

```text
PUT    /users/chats/{session_id}/shares/{viewer_user_id}
       Grant read access idempotently.

GET    /users/chats/{session_id}/shares
       List users who currently have access. Owner only.

DELETE /users/chats/{session_id}/shares/{viewer_user_id}
       Revoke access idempotently.

GET    /users/chats/list_chats?scope=all|owned|shared
       List authorized chat metadata through one ordered cursor.

GET    /users/chats/shared
       List the current viewer's grants with sharer identity for settings.

GET    /users/chats/{session_id}/messages
       Read history when the caller is owner or viewer.
```

### 6.1 Grant

Granting an existing grant is a successful no-op. The response returns the effective share record. The database primary key remains the final concurrency guard when two requests race.

Expected failures:

- `404`: session is missing, deleted, or not owned by the caller;
- `404`: target user does not exist in the caller's organisation;
- `400`: caller attempts to share with the owner; and
- `403`: authenticated caller is not permitted to manage the session.

### 6.2 Revoke

Revocation removes the access row and returns `204 No Content`. A subsequent session, message, or attachment request by that viewer must fail.

An audio URL issued before revocation remains usable until its storage signature expires. Shared-viewer playback URLs should therefore be short-lived.

### 6.3 Chat representations

Session responses should expose capability rather than requiring the frontend to reproduce authorization rules:

```json
{
  "session_id": "...",
  "title": "...",
  "access": "owner",
  "capabilities": {
    "read": true,
    "send": true,
    "rename": true,
    "delete": true,
    "manage_shares": true
  }
}
```

For a shared viewer, only `read` is true. The backend remains authoritative even if a client incorrectly displays a write control.

## 7. Future Messages

Viewers read the original `ChatSession` and `ChatMessage` rows. No message fan-out or synchronization is required.

V1 can use bounded polling:

1. Load the latest message page.
2. Keep a high-water cursor containing `(created_at, message_id)`.
3. Poll every few seconds for committed messages after that cursor.
4. Stop polling when authorization fails or the page is no longer active.

The existing message-job SSE endpoint is owner/job-specific and uses consume-once delivery. It must not be used for shared viewers because multiple consumers could interfere with one another and intermediate processing frames are not part of the durable shared transcript.

If polling later becomes a measured problem, add a session-level broadcast that publishes only a `messages_changed` signal. Clients should still fetch authorized canonical rows from PostgreSQL; the event channel must not carry chat bodies, attachments, or signed URLs.

## 8. Audio And Attachments

An authorized viewer receives attachments only through an authorized session-message path:

```text
caller -> readable session -> message in session -> attachment link -> signed URL
```

Do not authorize a shared attachment by comparing `Attachment.user_id` with the viewer. The attachment still belongs to the uploader. Session access is what permits the viewer to read that attachment in this chat.

Other requirements:

- storage remains private;
- storage paths are never returned directly;
- playback and thumbnail URLs are generated only after session authorization;
- shared-viewer URLs use a short expiry appropriate for revocation risk;
- deleted or failed attachments remain unavailable; and
- logs must not contain message text, raw audio, or signed URLs.

Realtime voice sessions currently persist transcription turns and audio-origin metadata. Sharing those rows makes the transcript visible. Sharing raw realtime recordings is a separate product and compliance requirement because it needs recording capture, consent, retention, and deletion policies.

## 9. Ownership And Lifecycle Rules

| Situation | Required behavior |
|---|---|
| Owner sends a new message | Existing viewers can read it after commit. |
| Viewer attempts to send | Reject as not found or forbidden; create no message/job. |
| Viewer attempts to mutate feedback or metadata | Reject; shared access is read-only. |
| Owner revokes a viewer | Future reads fail immediately; existing signed URLs expire naturally. |
| Owner deletes the chat | Session becomes unavailable to owner and all viewers. |
| Viewer is deactivated or leaves the organisation | Access checks fail even if a stale row remains. |
| Owner is deactivated | Apply the product's existing session-retention policy; do not transfer ownership implicitly. |
| Duplicate grant requests | Converge on one access row. |
| Concurrent grant and revoke | Transaction completion order determines final access state. |

## 10. Security And Privacy

- Every query is scoped to the authenticated caller's organisation.
- The grantee is validated against the same organisation before insert.
- UUID knowledge alone never grants session, message, job-stream, or attachment access.
- Only the owner can enumerate or change the viewer list.
- Shared viewers cannot access processing jobs or transient agent frames.
- Grant, revoke, denied access, and audio playback should produce structured audit events without content payloads.
- Rate limits apply to share mutation, shared-chat listing, polling, and URL generation.
- The UI must clearly state that sharing exposes the full evolving chat, including future messages and attachments, until revoked.

## 11. Query And Scale Characteristics

The primary read paths remain indexed relational queries:

```text
Can this user read this chat?
    primary-key lookup by (session_id, viewer_user_id)

Which chats are shared with this user?
    index scan by (organisation_id, viewer_user_id, created_at)

Who can read this chat?
    primary-key prefix scan by session_id
```

The table grows with actual grants rather than with all organisation-user/chat combinations. No cache is required initially. If authorization lookup volume later becomes material, measure it before adding a cache; PostgreSQL remains the source of truth and revocation correctness takes priority over avoiding a small indexed lookup.

## 12. Walking Skeleton

The smallest end-to-end slice is:

1. Add `chat_session_access` and its migration contract test.
2. Add the shared readable-session query helper and retain the owner-only helper.
3. Add idempotent grant and revoke endpoints.
4. Include owned and shared metadata in one list with optional access filtering.
5. Allow a viewer to list committed messages and receive authorized attachment playback URLs.
6. Prove the viewer cannot send or mutate anything.
7. Prove a newly committed owner message appears to the viewer without cloning.
8. Prove revocation blocks session, message, and attachment access.

This validates the risky boundaries before adding notifications or more responsive refresh behavior.

## 13. Test And Completion Gates

### Data model

- duplicate `(session_id, viewer_user_id)` grants cannot create two rows;
- deleting a session removes its access rows; and
- the unified list applies owner-or-grant authorization before its stable cursor.

### Authorization

- owner can read and write;
- selected same-organisation viewer can read;
- viewer cannot send, rename, delete, favorite, give feedback, share, or revoke;
- unselected same-organisation user cannot read;
- cross-organisation user cannot be granted or read;
- deleted sessions are inaccessible; and
- deactivated users cannot use stale grants.

### Messages and live continuity

- viewer sees existing messages;
- viewer sees a message committed after the grant;
- message ordering and cursor boundaries do not duplicate or skip rows;
- shared polling never exposes another session; and
- viewers cannot consume owner processing-job events.

### Audio and attachments

- viewer receives playback only for an attachment linked to an authorized message;
- viewer cannot access another attachment belonging to the same uploader;
- revocation blocks generating new playback URLs;
- expired URLs stop working according to storage policy; and
- raw storage paths and signed URLs are absent from logs.

## 14. Deferred Decisions

Do not implement these until there is an approved requirement:

- editor/commenter permissions;
- organisation-wide sharing;
- manager hierarchy or automatic manager access;
- team/group principals;
- public or external share links;
- expiring grants;
- ownership transfer;
- cloned/forked conversations;
- per-message sharing;
- session-level push notifications; and
- raw realtime voice recording retention.

Each can be added without changing the V1 ownership rule or copying chat messages.

## 15. Final Architecture Test

The design is complete when this statement is true:

```text
A chat has exactly one owner and one durable message history. Selected users gain
read access through explicit relational grants, see future committed messages, and
cannot create or mutate chat data. Removing the grant removes future access without
copying, synchronizing, or rewriting the chat.
```

## 16. Implementation Verification

Verified on 2026-07-17 with the focused migration, service, security-boundary, SSE, voice, and API suite:

```powershell
uv run pytest tests/migrations/org/test_079_chat_session_access.py tests/migrations/test_org_migration_head_contract.py tests/service/chat/test_chat_access.py tests/service/chat/test_chat_session_service.py tests/service/chat/test_chat_session_sharing_service.py tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py tests/service/chat/test_chat_message_sse_service.py tests/service/voice_realtime/test_turn_persistence_service.py tests/api/routers/chat/test_chat_sessions_handler.py tests/api/routers/chat/test_chat_sharing_handler.py tests/api/routers/chat/test_chat_messages_handler.py tests/api/routers/chat/test_voice_realtime_handler.py -q -o log_cli=false
```

Observed result: `179 passed, 4 warnings`.

Formatting and lint were verified across the feature's Python files:

```powershell
uv run ruff format --check src/domain/models/organisation_tables/chat_session_access_models.py src/service/chat/chat_access.py src/service/chat/chat_session_service.py src/service/chat/chat_message_service.py src/service/chat/chat_message_sse_service.py src/api/schemas/chat_models.py src/api/routers/chat/sessions_handler.py src/api/routers/chat/messages_handler.py tests/migrations/org/test_079_chat_session_access.py tests/service/chat/test_chat_access.py tests/service/chat/test_chat_session_sharing_service.py tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py tests/service/chat/test_chat_message_sse_service.py tests/api/routers/chat/test_chat_sharing_handler.py tests/api/routers/chat/test_chat_messages_handler.py
uv run ruff check src/domain/models/organisation_tables/chat_session_access_models.py src/service/chat/chat_access.py src/service/chat/chat_session_service.py src/service/chat/chat_message_service.py src/service/chat/chat_message_sse_service.py src/api/schemas/chat_models.py src/api/routers/chat/sessions_handler.py src/api/routers/chat/messages_handler.py tests/migrations/org/test_079_chat_session_access.py tests/service/chat/test_chat_access.py tests/service/chat/test_chat_session_sharing_service.py tests/service/chat/test_chat_message_service.py tests/service/chat/test_chat_security_boundaries.py tests/service/chat/test_chat_message_sse_service.py tests/api/routers/chat/test_chat_sharing_handler.py tests/api/routers/chat/test_chat_messages_handler.py
```

Observed result: all `16` files formatted and all checks passed.

Diff hygiene was verified with:

```powershell
git diff --check
```

Observed result: exit code `0`; Git reported only an unrelated working-tree line-ending warning for `tests/scripts/test_convert_ccci_sales_report.py`.
