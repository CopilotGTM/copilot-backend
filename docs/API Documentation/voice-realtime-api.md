# Voice Realtime API Documentation

This document covers the dedicated APIs used by the mobile voice realtime experience: bootstrapping a short-lived Azure realtime session, attaching backend sideband control to the live call, persisting completed voice turns, and retrieving voice session history/results through the existing chat APIs.

Route exposure is deployment-controlled via `VOICE_REALTIME_API_ENABLED`. In production this flag
should remain `false`, which means these routes are not mounted and return `404 Not Found`.

`VOICE_REALTIME_EXPANDED_TOOLS_ENABLED` defaults to `true`. Set it to `false` and start a new
realtime session to restore the pre-expansion manifest while leaving the voice APIs available.
`VOICE_REALTIME_PUBLIC_WEB_TOOLS_ENABLED` defaults to `false`; public search and crawling remain
hidden until the deployment explicitly accepts the public-query boundary.

## Base URLs

```text
/users/realtime        # Voice realtime session bootstrap
/users/chats           # Voice session persistence and transcript retrieval
```

---

## Response Envelope

All endpoints return a consistent envelope:

```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human-readable message",
  "data": { ... } | null
}
```

- Timestamps use ISO 8601 with `Z` suffix (UTC), e.g. `2026-04-11T10:45:00Z`.

## Authentication

- Required for all endpoints in this document
- Header: `Authorization: Bearer <access_token>`

## Rate Limiting

- Write endpoints in this flow use `rate_limit_user_write`
- On limit breach: `429 Too Many Requests` with `Retry-After` header

---

## Overview

Voice realtime uses two backend write endpoints and two existing chat read endpoints:

1. `POST /users/realtime/session`
   Returns an Azure realtime `ephemeral_key`, a backend `chat_session_id`, and a
   backend-authored `runtime_config`.
2. `POST /users/realtime/calls/attach`
   Attaches backend sideband control to the live realtime `call_id` so the backend
   owns prompt injection and tool execution.
3. `POST /users/chats/voice/turns`
   Persists completed user and assistant turns into the normal chat message store.
4. `GET /users/chats/list_chats?chat_type=VOICE_CHAT`
   Lists prior voice sessions.
5. `GET /users/chats/messages/list?session_id=<chat_session_id>`
   Returns the persisted transcript/results for a voice session.

The backend never proxies audio. The client connects directly to Azure Realtime using the
`endpoint` and `ephemeral_key` returned by the bootstrap endpoint, but the backend remains the
source of truth for the voice prompt and allowed tool contract.

The expanded manifest adds public search and crawling, deal memory and stakeholder lookup,
meeting update and cancellation, connected-mailbox discovery and search, email attachment reading,
and provider draft save/send workflows. These tools reuse the General Agent's org-scoped backend
implementations; the realtime prompt supplies the voice-specific routing and confirmation policy.
Direct `send_email` is not advertised to realtime. New email delivery follows the provider-draft
review path, and external mutations require a new user audio turn after their preview.

---

## Data Types

### Request Models

```ts
type UUID = string;

export type RealtimeDevicePlatform = "android" | "ios" | "web";
export type RealtimeConversationMode = "push_to_talk";
export type RealtimeTurnMode = "push_to_talk";
export type RealtimeToolPolicy = "guardrailed_auto";
export type RealtimeControlMode = "backend_sideband";

export interface UserRealtimeSessionRequest {
  device: {
    platform: RealtimeDevicePlatform;
    app_version: string;
  };
  conversation_mode: RealtimeConversationMode;
  chat_session_id?: UUID | null;
}

export interface UserRealtimeRuntimeConfig {
  chat_session_id: UUID;
  control_mode: RealtimeControlMode;
  tool_policy: RealtimeToolPolicy;
  allowed_tools: string[];
  session_update: {
    type: "session.update";
    session: {
      modalities: ("text" | "audio")[];
      instructions: string;
      tools: Array<{
        type: "function";
        name: string;
        description: string;
        parameters: Record<string, unknown>;
      }>;
      audio: {
        input: {
          format: { type: "audio/pcm"; sample_rate_hz: number };
          transcription: { model: "whisper-1" };
        };
        output: {
          format: { type: "audio/pcm"; sample_rate_hz: number };
          voice: string;
        };
      };
      turn_detection: null;
    };
  };
  issued_at: string;
  expires_at: string;
  signature: string;
}

export interface UserRealtimeCallAttachRequest {
  session_id: string;
  call_id: string;
  chat_session_id: UUID;
  runtime_config: UserRealtimeRuntimeConfig;
}

export interface UserRealtimeCallAttachResponse {
  call_id: string;
  chat_session_id: UUID;
  control_mode: RealtimeControlMode;
  status: "attached";
}

export interface VoiceTurnEntry {
  realtime_item_id: string;
  speaker: "user" | "assistant";
  text: string;
  turn_index: number;
}

export interface PersistVoiceTurnsRequest {
  chat_session_id: UUID;
  turns: VoiceTurnEntry[];
}
```

### Response Models

```ts
export interface UserRealtimeSessionResponse {
  session_id: string;      // Azure realtime session id
  ephemeral_key: string;   // short-lived Azure client secret
  endpoint: string;        // Azure realtime calls endpoint
  model: string;
  voice: string;
  expires_at: string;      // ISO8601
  turn_mode: RealtimeTurnMode;
  chat_session_id: UUID;   // backend ChatSession id
  runtime_config: UserRealtimeRuntimeConfig;
}

export interface PersistVoiceTurnsResponse {
  persisted_count: number;
  message_ids: UUID[];
}
```

---

## 1) Create Realtime Session

### Endpoint

```http
POST /users/realtime/session
```

### Description

Creates a short-lived Azure realtime client secret for the authenticated user and returns the
backend `chat_session_id` used to persist and later retrieve the transcript.

The response also includes `runtime_config`, which is the backend-authored voice runtime contract.
It contains the injected prompt, realtime tool schemas, allowed tool policy metadata, and the
exact `session.update` payload the backend sideband connection will apply after the live call is
attached.

Behavior:

- If `chat_session_id` is omitted or `null`, the backend creates a new `ChatSession` with `chat_type = VOICE_CHAT`.
- If `chat_session_id` is provided, the backend reuses that backend session id and returns a fresh Azure realtime session for reconnect or resume scenarios.

### Request Body

```json
{
  "device": {
    "platform": "android",
    "app_version": "1.0.0"
  },
  "conversation_mode": "push_to_talk",
  "chat_session_id": null
}
```

### Request Fields

| Field | Type | Required | Notes |
|------|------|----------|-------|
| `device.platform` | string | Yes | `"android"`, `"ios"`, or `"web"` |
| `device.app_version` | string | Yes | Client app version for telemetry |
| `conversation_mode` | string | Yes | Currently only `"push_to_talk"` |
| `chat_session_id` | string \| null | No | Existing backend voice session id to resume |

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Realtime session created successfully.",
  "data": {
    "session_id": "azure-session-abc123",
    "ephemeral_key": "ek_abc123",
    "endpoint": "https://example.openai.azure.com/openai/v1/realtime/calls",
    "model": "gpt-realtime-mini",
    "voice": "alloy",
    "expires_at": "2026-04-11T10:45:00Z",
    "turn_mode": "push_to_talk",
    "chat_session_id": "550e8400-e29b-41d4-a716-446655440000",
    "runtime_config": {
      "chat_session_id": "550e8400-e29b-41d4-a716-446655440000",
      "control_mode": "backend_sideband",
      "tool_policy": "guardrailed_auto",
      "allowed_tools": [
        "get_deal_details",
        "get_meeting_summary",
        "list_deal_notes",
        "search_internal_knowledge"
      ],
      "session_update": {
        "type": "session.update",
        "session": {
          "modalities": ["text", "audio"],
          "instructions": "You are Kai, CopilotGTM's AI sales assistant...",
          "tools": [
            {
              "type": "function",
              "name": "search_internal_knowledge",
              "description": "Search organisation knowledge and documents.",
              "parameters": {
                "type": "object",
                "properties": {
                  "query": {
                    "type": "string"
                  },
                  "limit": {
                    "type": "integer"
                  }
                },
                "required": ["query"],
                "additionalProperties": false
              }
            }
          ],
          "audio": {
            "input": {
              "format": {
                "type": "audio/pcm",
                "sample_rate_hz": 16000
              },
              "transcription": {
                "model": "whisper-1"
              }
            },
            "output": {
              "format": {
                "type": "audio/pcm",
                "sample_rate_hz": 24000
              },
              "voice": "alloy"
            }
          },
          "turn_detection": null
        }
      },
      "issued_at": "2026-04-13T09:00:00Z",
      "expires_at": "2026-04-13T10:00:00Z",
      "signature": "8f7d0f3f..."
    }
  }
}
```

### Response Notes

- `session_id` is the Azure realtime session id.
- `ephemeral_key` is the short-lived Azure client secret the mobile client uses for direct WebRTC setup.
- `endpoint` is the Azure realtime calls URL.
- `chat_session_id` is the backend chat session id that must be reused in persistence and retrieval calls.
- `turn_mode` currently always resolves to `"push_to_talk"`.
- `runtime_config.control_mode` currently always resolves to `"backend_sideband"`.
- `runtime_config.session_update` is server-authored and is applied by the backend voice worker after the live call is attached.
- The frontend must not inject its own voice prompt or build its own tool list for this session.
- `allowed_tools` is backend-owned policy metadata.
- `session_update.session.tools` contains the realtime function schemas that match the backend-owned tool subset.
- `signature` is an opaque backend signature for tamper detection and auditing.

### Frontend Integration Contract

1. Call `POST /users/realtime/session`.
2. Connect directly to Azure Realtime using `endpoint` and `ephemeral_key`.
3. Once the live `call_id` is available, call `POST /users/realtime/calls/attach` with `session_id`, `call_id`, `chat_session_id`, and `runtime_config`.
4. Do not construct prompts, tool definitions, or tool outputs on the frontend.
5. Persist completed transcript turns with `POST /users/chats/voice/turns`.

The frontend should treat `runtime_config` as server-authored configuration, not as a template to
modify.

### Error Responses

- 500 Internal Server Error — "Realtime service is not configured."
- 502 Bad Gateway — "Failed to create realtime session."
- 502 Bad Gateway — "Realtime provider returned an invalid session response."
- 503 Service Unavailable — "Realtime service is temporarily unavailable."
- 500 Internal Server Error — "An unexpected error occurred during realtime session creation."

### Example cURL

```bash
curl -X POST "$BASE_URL/users/realtime/session" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "device": {
      "platform": "android",
      "app_version": "1.0.0"
    },
    "conversation_mode": "push_to_talk",
    "chat_session_id": null
  }'
```

---

## 2) Attach Realtime Call

### Endpoint

```http
POST /users/realtime/calls/attach
```

### Description

Queues backend sideband control for the live Azure realtime call after the frontend has completed
the direct WebRTC connection. This is the point where the backend becomes the active owner of:

- prompt injection
- realtime tool schema registration
- function-call handling
- backend tool execution

### Request Body

```json
{
  "session_id": "azure-session-abc123",
  "call_id": "call_live_123",
  "chat_session_id": "550e8400-e29b-41d4-a716-446655440000",
  "runtime_config": {
    "chat_session_id": "550e8400-e29b-41d4-a716-446655440000",
    "control_mode": "backend_sideband",
    "tool_policy": "guardrailed_auto",
    "allowed_tools": ["search_internal_knowledge"],
    "session_update": {
      "type": "session.update",
      "session": {
        "modalities": ["text", "audio"],
        "instructions": "You are Kai...",
        "tools": []
      }
    },
    "issued_at": "2026-04-13T09:00:00Z",
    "expires_at": "2026-04-13T10:00:00Z",
    "signature": "8f7d0f3f..."
  }
}
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Realtime call attached successfully.",
  "data": {
    "call_id": "call_live_123",
    "chat_session_id": "550e8400-e29b-41d4-a716-446655440000",
    "control_mode": "backend_sideband",
    "status": "queued"
  }
}
```

The attach response returns after validation, persistence, and worker dispatch succeed. The
long-lived sideband websocket and backend tool execution then run inside the chat worker on the
dedicated `voice_realtime` queue.

### Error Responses

- 400 Bad Request — "Realtime runtime configuration is invalid."
- 400 Bad Request — "Realtime runtime configuration has expired."
- 404 Not Found — "Voice chat session not found."
- 409 Conflict — "Realtime call is already attached to another session."
- 500 Internal Server Error — "Realtime service is not configured."
- 500 Internal Server Error — "An unexpected error occurred during realtime call attachment."

---

## 3) Persist Voice Turns

### Endpoint

```http
POST /users/chats/voice/turns
```

### Description

Persists completed voice turns as normal `ChatMessage` rows under the backend `chat_session_id`.

Persistence rules:

- The backend verifies that the session belongs to the authenticated user and organisation.
- The session must have `chat_type = VOICE_CHAT`.
- Soft-deleted sessions are rejected.
- The backend deduplicates turns by `realtime_item_id` within the same session.
- If the session title is still `"Voice Session"`, the backend retitles it from the first persisted user utterance, truncated to 60 characters.

### Request Body

```json
{
  "chat_session_id": "550e8400-e29b-41d4-a716-446655440000",
  "turns": [
    {
      "realtime_item_id": "item_user_001",
      "speaker": "user",
      "text": "How is the Acme deal looking?",
      "turn_index": 0
    },
    {
      "realtime_item_id": "item_assistant_001",
      "speaker": "assistant",
      "text": "The Acme deal is currently in negotiation and the main blocker is pricing.",
      "turn_index": 1
    }
  ]
}
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Persisted 2 voice turns.",
  "data": {
    "persisted_count": 2,
    "message_ids": [
      "11111111-1111-1111-1111-111111111111",
      "22222222-2222-2222-2222-222222222222"
    ]
  }
}
```

### Idempotency

This endpoint is idempotent at the turn level. Re-sending a turn with the same `realtime_item_id` for the same `chat_session_id` does not create a duplicate message.

### Stored Message Shape

Each persisted turn is stored as a normal `ChatMessage`:

```json
{
  "role": "user | assistant",
  "content": {
    "text": "transcribed text",
    "source": "voice_transcription",
    "realtime_item_id": "item_user_001"
  },
  "extra": {
    "turn_index": 0
  }
}
```

### Error Responses

- 404 Not Found — "Voice chat session not found."
- 500 Internal Server Error — "Failed to persist voice turns."

### Example cURL

```bash
curl -X POST "$BASE_URL/users/chats/voice/turns" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "chat_session_id": "550e8400-e29b-41d4-a716-446655440000",
    "turns": [
      {
        "realtime_item_id": "item_user_001",
        "speaker": "user",
        "text": "How is the Acme deal looking?",
        "turn_index": 0
      },
      {
        "realtime_item_id": "item_assistant_001",
        "speaker": "assistant",
        "text": "The Acme deal is currently in negotiation and the main blocker is pricing.",
        "turn_index": 1
      }
    ]
  }'
```

---

## 4) List Voice Sessions

### Endpoint

```http
GET /users/chats/list_chats?chat_type=VOICE_CHAT&limit=20&cursor=<opaque>
```

### Description

Returns only voice sessions by using the new `chat_type` filter on the existing chat list endpoint.
Sessions are ordered by `updated_at`; persisted user voice turns bump the session.

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Chats listed",
  "data": {
    "items": [
      {
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
        "title": "How is the Acme deal looking?",
        "chat_type": "VOICE_CHAT",
        "is_favorite": false,
        "tags": [],
        "created_at": "2026-04-11T10:44:00Z",
        "updated_at": "2026-04-11T10:45:00Z"
      }
    ],
    "cursor": null
  }
}
```

### Notes

- `chat_type=VOICE_CHAT` is optional from the API’s perspective, but required if the client wants only voice sessions.
- The `session_id` returned here is the backend `chat_session_id`.

### Error Responses

- 500 Internal Server Error — "Unable to list chat sessions"

---

## 5) Get Voice Transcript and Results

### Endpoint

```http
GET /users/chats/messages/list?session_id=<chat_session_id>&limit=20&cursor=<opaque>
```

### Description

Returns the persisted transcript for a voice session through the normal chat messages API.

For `VOICE_CHAT` sessions:

- User turns appear as `role = "user"`
- Kai responses appear as `role = "assistant"`
- The underlying stored content comes from `POST /users/chats/voice/turns`

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Messages listed",
  "data": {
    "items": [
      {
        "message_id": "11111111-1111-1111-1111-111111111111",
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
        "role": "user",
        "message_type": "GENERAL_MESSAGE",
        "content": "How is the Acme deal looking?",
        "is_edited": false,
        "created_at": "2026-04-11T10:44:31Z",
        "updated_at": "2026-04-11T10:44:31Z",
        "attachments": [],
        "confidence_score": null,
        "sources": null,
        "suggestions": null,
        "feedback_score": null
      },
      {
        "message_id": "22222222-2222-2222-2222-222222222222",
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
        "role": "assistant",
        "message_type": "GENERAL_MESSAGE",
        "content": "The Acme deal is currently in negotiation and the main blocker is pricing.",
        "is_edited": false,
        "created_at": "2026-04-11T10:44:36Z",
        "updated_at": "2026-04-11T10:44:36Z",
        "attachments": [],
        "confidence_score": null,
        "sources": null,
        "suggestions": null,
        "feedback_score": null
      }
    ],
    "cursor": null
  }
}
```

### Notes

- This is how the client gets voice conversation results after persistence.
- The endpoint is not voice-specific, but `VOICE_CHAT` sessions use it as the canonical transcript/history API.

### Error Responses

- 400 Bad Request — invalid session or filters
- 500 Internal Server Error — "Unable to list messages"

---

## End-to-End Flow

1. `POST /users/realtime/session`
   The backend returns `ephemeral_key`, `endpoint`, `chat_session_id`, and `runtime_config`.
2. The client connects directly to Azure Realtime using `endpoint` and `ephemeral_key`.
3. Once connected, the client captures the live `call_id`.
4. The client calls `POST /users/realtime/calls/attach` so the backend can open a sideband
   connection, send `session.update`, and handle tool calls.
5. After each completed user/assistant turn, the client calls `POST /users/chats/voice/turns`.
6. To show history, the client calls `GET /users/chats/list_chats?chat_type=VOICE_CHAT`.
7. To show the actual transcript/results, the client calls `GET /users/chats/messages/list?session_id=<chat_session_id>`.

---

## Related Documents

- `docs/Technical Documentation/voice-realtime-architechture.md` — full architecture, WebRTC flow, security, and data model details
- `docs/API Documentation/chat-api.md` — broader chat APIs, including shared list/message endpoints

---

## Changelog

### 2026-04-13

- Documented the new `runtime_config` field returned by `POST /users/realtime/session`.
- Added `control_mode="backend_sideband"` and realtime `session.tools` to the signed voice runtime contract.
- Added `POST /users/realtime/calls/attach` for backend sideband ownership of the live call.
- Updated the frontend contract to hand off the live `call_id` instead of applying `session.update` locally.
- Clarified that voice prompt injection and tool policy are backend-owned and that tools do not run
  in the frontend app.
- Moved live voice sideband ownership out of the API process and into the chat worker on the
  dedicated `voice_realtime` queue.
