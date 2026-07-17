# Read-Only Chat Sharing API

Frontend contract for sharing an existing chat with selected users in the same organisation.
The shared chat is not cloned: owners and viewers read the same session, messages, and
attachments. The owner remains the only user who can mutate the chat.

## Base Path And Authentication

```text
/users/chats
```

All requests require `Authorization: Bearer <access_token>`. Except for successful
`204 No Content` responses and the SSE-specific `404`, responses use the standard envelope:

```ts
interface ApiEnvelope<T> {
  status: "SUCCESS" | "ERROR";
  message: string;
  data: T | null;
}
```

## Frontend Types

```ts
type UUID = string;
type ChatAccess = "owner" | "viewer";

interface ChatSessionCapabilities {
  read: boolean;
  send: boolean;
  rename: boolean;
  delete: boolean;
  manage_shares: boolean;
}

interface ChatSessionOut {
  session_id: UUID;
  title: string;
  chat_type: string;
  is_favorite: boolean;
  tags: string[];
  created_at: string;
  updated_at: string;
  access: ChatAccess;
  capabilities: ChatSessionCapabilities;
}

interface ChatSessionsPage {
  items: ChatSessionOut[];
  cursor: string | null;
}

interface ChatSessionShareOut {
  session_id: UUID;
  viewer_user_id: UUID;
  granted_by_user_id: UUID;
  created_at: string;
}

interface SharedByUserOut {
  user_id: UUID;
  name: string | null;
  email: string | null;
}

interface SharedChatOut extends ChatSessionOut {
  shared_at: string;
  shared_by: SharedByUserOut;
}

interface SharedChatsPage {
  items: SharedChatOut[];
  cursor: string | null;
}
```

Owner sessions return:

```json
{
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

Shared sessions return:

```json
{
  "access": "viewer",
  "capabilities": {
    "read": true,
    "send": false,
    "rename": false,
    "delete": false,
    "manage_shares": false
  }
}
```

Use `capabilities` to render controls. Do not infer permissions from ownership IDs or
`chat_type`; the backend remains authoritative if a stale client displays a forbidden action.

## Endpoint Summary

| Method | Endpoint | Caller | Purpose |
|---|---|---|---|
| `GET` | `/users/chats/list_chats?scope=all|owned|shared` | Owner or viewer | List authorized chat metadata with one cursor. |
| `GET` | `/users/chats/shared` | Viewer | List chats shared with the caller, including who shared them. |
| `PUT` | `/users/chats/{session_id}/shares/{viewer_user_id}` | Owner | Grant read access idempotently. |
| `GET` | `/users/chats/{session_id}/shares` | Owner | List current viewer grants. |
| `DELETE` | `/users/chats/{session_id}/shares/{viewer_user_id}` | Owner | Revoke read access idempotently. |
| `GET` | `/users/chats/messages/list?session_id={session_id}` | Owner or viewer | Read canonical committed messages and authorized attachments. |

`viewer_user_id` must come from the application's authenticated organisation-user directory.
The sharing API does not accept email addresses and does not provide user search.

## List Authorized Chats

```http
GET /users/chats/list_chats?scope=all&limit=20&cursor=<opaque>
```

Query parameters:

- `limit`: optional integer, default `20`, range `1..100`.
- `cursor`: optional opaque cursor returned by the previous page. Pass it back unchanged.
- `scope`: optional `all`, `owned`, or `shared`; default `all`.
- `chat_type`: optional exact chat-type filter.

The backend applies authorization before ordering and pagination. Owned and shared rows use one
canonical `(updated_at, session_id)` cursor; the frontend must not merge independently paginated
collections. Reset the cursor whenever `scope` or `chat_type` changes. Deleted sessions and
`TEAMS_CHAT` sessions are excluded.

```json
{
  "status": "SUCCESS",
  "message": "Chats listed",
  "data": {
    "items": [
      {
        "session_id": "0fddbc2d-2382-47ec-a787-b56949905ec7",
        "title": "Customer renewal discussion",
        "chat_type": "GENERAL_CHAT",
        "is_favorite": false,
        "tags": [],
        "created_at": "2026-07-17T08:10:00Z",
        "updated_at": "2026-07-17T09:20:00Z",
        "access": "viewer",
        "capabilities": {
          "read": true,
          "send": false,
          "rename": false,
          "delete": false,
          "manage_shares": false
        }
      }
    ],
    "cursor": null
  }
}
```

Errors: `401`, `429`, or `500` with the standard envelope.

## List Chats Shared With Me

Use this separate projection for the settings screen when the UI needs sharing attribution.
The unified `list_chats` endpoint remains the sidebar collection and intentionally does not
include `shared_by` metadata.

```http
GET /users/chats/shared?limit=20&cursor=<opaque>
```

Query parameters:

- `limit`: optional integer, default `20`, range `1..100`.
- `cursor`: optional opaque cursor returned by the previous page. Pass it back unchanged.

Results are ordered by `(shared_at, session_id)` descending. The cursor belongs only to this
endpoint and must not be reused with `list_chats`. Revoked grants, deleted chats, and
`TEAMS_CHAT` sessions are excluded.

```json
{
  "status": "SUCCESS",
  "message": "Shared chats listed",
  "data": {
    "items": [
      {
        "session_id": "0fddbc2d-2382-47ec-a787-b56949905ec7",
        "title": "Customer renewal discussion",
        "chat_type": "GENERAL_CHAT",
        "is_favorite": false,
        "tags": [],
        "created_at": "2026-07-17T08:10:00Z",
        "updated_at": "2026-07-17T09:20:00Z",
        "access": "viewer",
        "capabilities": {
          "read": true,
          "send": false,
          "rename": false,
          "delete": false,
          "manage_shares": false
        },
        "shared_at": "2026-07-17T09:30:00Z",
        "shared_by": {
          "user_id": "6b591b8d-1ea7-46e8-8ccd-7fc90b64ce91",
          "name": "Avery Owner",
          "email": "avery.owner@example.com"
        }
      }
    ],
    "cursor": null
  }
}
```

`shared_by.user_id` always comes from the grant. `name` and `email` can be `null` if the
sharer's user profile no longer exists; the shared chat remains visible. Errors are `401`,
`429`, or `500` with the standard envelope.

## Grant A Viewer

```http
PUT /users/chats/{session_id}/shares/{viewer_user_id}
```

There is no request body. Repeating the same request is safe and returns the existing grant.
The target must be an active, approved user in the owner's organisation.

```json
{
  "status": "SUCCESS",
  "message": "Chat viewer granted",
  "data": {
    "session_id": "0fddbc2d-2382-47ec-a787-b56949905ec7",
    "viewer_user_id": "4953da73-ff1a-470e-b04c-8087eccc8684",
    "granted_by_user_id": "6b591b8d-1ea7-46e8-8ccd-7fc90b64ce91",
    "created_at": "2026-07-17T09:30:00Z"
  }
}
```

Errors:

- `400`: attempting to add the chat owner (`"Chat owner already has access"`).
- `404`: invalid viewer, inaccessible chat, deleted chat, or non-owner caller. The message is
  deliberately generic: `"Chat session or viewer not found"`.
- `401`, `429`, `500`: standard envelope.

## List Current Viewers

```http
GET /users/chats/{session_id}/shares
```

This owner-only endpoint is not paginated.

```json
{
  "status": "SUCCESS",
  "message": "Chat viewers listed",
  "data": [
    {
      "session_id": "0fddbc2d-2382-47ec-a787-b56949905ec7",
      "viewer_user_id": "4953da73-ff1a-470e-b04c-8087eccc8684",
      "granted_by_user_id": "6b591b8d-1ea7-46e8-8ccd-7fc90b64ce91",
      "created_at": "2026-07-17T09:30:00Z"
    }
  ]
}
```

Errors: `404` with `"Chat session or viewer not found"` for a missing, deleted, or unowned
chat; otherwise `401`, `429`, or `500`.

## Revoke A Viewer

```http
DELETE /users/chats/{session_id}/shares/{viewer_user_id}
```

Success is `204 No Content` with an empty body. Repeating the delete is safe while the caller
still owns an accessible chat. A missing, deleted, or unowned chat returns `404` with the
standard generic share-management error.

After revocation, new session, message, and attachment requests by that viewer fail. A signed
attachment URL issued before revocation may remain usable until its short expiry, so the
frontend must not persist signed URLs.

## Read A Shared Chat

Use the existing message endpoint:

```http
GET /users/chats/messages/list?session_id={session_id}&limit=20&cursor=<opaque>
```

An explicitly granted viewer receives the same committed message rows as the owner, including
attachments authorized through that session. The response shape is `ApiEnvelope<ChatMessagesPage>`
as documented in [chat-api.md](chat-api.md#10-list-messages-cursor-based).

Important frontend behavior:

- `cursor` paginates toward older messages; it is not a live-update cursor.
- `data.session` contains the current caller's `access` and `capabilities`. Treat it as the source
  of truth on direct loads and reloads; sidebar metadata is only an initial UI optimization.
- To refresh, fetch the first page again and merge by `message_id`.
- A new owner message appears in the original session after it commits; no clone synchronization
  is required.
- Revoked or otherwise unauthorized reads currently return `400` with
  `message: "Chat session not found"` from this endpoint.
- Attachment playback and thumbnail URLs are short-lived. Use the newest URLs from each refresh.

## Owner-Only Operations

A viewer must not call or expose controls for:

- `POST /users/chats/send_message`
- `PATCH /users/chats/rename_chat`
- `DELETE /users/chats/delete_chat`
- `PATCH /users/chats/favorite_chat`
- `POST /users/chats/messages/feedback`
- share grant/list/revoke endpoints
- voice-turn persistence
- `GET /users/chats/message_events`

The message-job SSE endpoint is owner-only because it contains transient processing frames.
An unowned or unknown `job_id` returns:

```http
HTTP/1.1 404 Not Found
```

```json
{ "detail": "Chat job not found" }
```

Viewers should refresh durable messages instead of connecting to SSE.

## Recommended Frontend Flow

### Owner

1. Render actions from `session.capabilities`.
2. Open the share picker only when `manage_shares` is `true`.
3. Load current grants with `GET /{session_id}/shares`.
4. Grant with `PUT /{session_id}/shares/{viewer_user_id}`.
5. Revoke with `DELETE /{session_id}/shares/{viewer_user_id}` and remove the row after `204`.

### Viewer

1. Load the default `GET /users/chats/list_chats` for the unified sidebar, or use
   `scope=shared` for a "Shared with me" filter.
2. Load `GET /users/chats/shared` in settings when sharer name/email is required.
3. Open a session with `GET /users/chats/messages/list`.
4. Keep composer, rename, favorite, delete, feedback, and share-management controls hidden or
   disabled according to `capabilities`.
5. Refresh the first message page periodically while the chat is open; merge by `message_id`.
6. Stop refreshing and remove the chat from local shared state after an authorization failure.

## Scope Limits

V1 supports explicit same-organisation viewers only. It does not support editor access,
organisation-wide access, teams/groups, public links, expiry, ownership transfer, or cloned chats.
