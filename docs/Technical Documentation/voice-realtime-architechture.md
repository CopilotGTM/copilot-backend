# Voice Realtime Architecture

Complete system architecture for the CopilotGTM voice realtime experience — how the mobile app, backend API, Azure OpenAI Realtime API, and persistence layer work together to deliver live voice conversations with Kai.

---

## 1. Overview

The voice realtime system lets sales reps talk to Kai (CopilotGTM's AI assistant) using
push-to-talk on their mobile device. Audio streams directly from the phone to Azure OpenAI's
Realtime API via WebRTC. The backend never touches audio, but it does own authentication, session
bootstrapping, worker dispatch, prompt injection, tool policy, and conversation persistence.

Key design decisions:
- **Audio bypasses the backend entirely.** The mobile app connects directly to Azure via WebRTC after the backend provisions a short-lived ephemeral key.
- **Voice sessions reuse the existing chat data model.** No new database tables — voice turns are stored as `ChatMessage` rows under a `ChatSession` with `chat_type=VOICE_CHAT`.
- **The backend owns the live realtime control plane.** The mobile app establishes media transport, then hands the live `call_id` to the backend so the chat worker can open a sideband connection, inject prompt/tool state, and handle function calls.
- **Push-to-talk only.** The user holds to speak, releases to send. No voice activity detection (VAD) — turn boundaries are explicit.

---

## 2. System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         MOBILE APP (React Native / Expo)             │
│                                                                      │
│  ┌─────────────┐   ┌──────────────────┐   ┌──────────────────────┐  │
│  │ AuthProvider │   │ VoiceHomeScreen  │   │ AzureRealtimeClient  │  │
│  │ (Supabase   │   │ (UI + session    │   │ (WebRTC transport)   │  │
│  │  JWT mgmt)  │   │  orchestration)  │   │                      │  │
│  └──────┬──────┘   └────────┬─────────┘   └──────────┬───────────┘  │
│         │                   │                        │               │
│         │  ┌────────────────┴────────────────┐       │               │
│         │  │ useVoiceSessionController       │       │               │
│         │  │ (XState machine + event handler) │◄──────┘               │
│         │  └────────────────┬────────────────┘                       │
│         │                   │                                        │
└─────────┼───────────────────┼────────────────────────────────────────┘
          │                   │
          │ JWT Bearer        │ 1. POST /users/realtime/session
          │ token             │ 2. POST /users/realtime/calls/attach
          │                   │ 3. POST /users/chats/voice/turns
          │                   │
          ▼                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    FASTAPI BACKEND (Python)                           │
│                                                                      │
│  ┌──────────────┐  ┌────────────────────┐  ┌─────────────────────┐  │
│  │ Auth Chain   │  │ RealtimeSession    │  │ VoiceRealtime       │  │
│  │ (Supabase +  │  │ Service            │  │ Handler             │  │
│  │  revocation  │  │ (ephemeral key +   │  │ (turn persistence   │  │
│  │  + rate lim) │  │  runtime contract) │  │  + dedup)           │  │
│  └──────┬───────┘  └────────┬───────────┘  └─────────┬───────────┘  │
└─────────┼───────────────────┼────────────────────────┼───────────────┘
          │                   │                        │
          ▼                   ▼                        ▼
┌─────────────────┐  ┌───────────────────┐  ┌──────────────────────┐
│ Supabase        │  │ Azure OpenAI      │  │ PostgreSQL           │
│ (JWT verify,    │  │ Realtime API      │  │ (per-org via         │
│  user mgmt)     │  │ (ephemeral keys,  │  │  pgbouncer)          │
│                 │  │  WebRTC endpoint) │  │                      │
└─────────────────┘  └────────┬──────────┘  └──────────┬───────────┘
                              │                         │
                              ▼                         ▼
                    ┌──────────────────────┐   ┌────────────────────┐
                    │ chat_worker          │   │ Redis              │
                    │ (voice_realtime      │   │ (broker + call     │
                    │  queue, sideband     │   │  ownership lock)   │
                    │  runtime, tool exec) │   │                    │
                    └──────────────────────┘   └────────────────────┘
                              │
                    WebRTC    │  Audio (PCM16) + Data Channel
                    (direct)  │
                              │
                     ┌────────┴────────┐
                     │  Mobile App     │
                     │  (audio I/O)    │
                     └─────────────────┘
```

**Infrastructure dependencies:**
- **Redis** — Rate limiting counters, token revocation store, pub/sub messaging
- **Azure Key Vault** — Production credentials (Supabase keys, Azure OpenAI API key, JWT secrets)
- **pgbouncer** — Connection pooling with role-based tenant isolation

---

## 3. Authentication and Security

### 3.1 JWT Token Chain

Every API request from the mobile app carries a Supabase JWT in the `Authorization: Bearer` header. The backend validates it through a multi-step chain:

```
Mobile sends: Authorization: Bearer <supabase_jwt>
                    │
                    ▼
        ┌───────────────────────┐
        │ 1. Format check       │  Token must have 3 dot-separated parts
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────┐
        │ 2. Supabase verify    │  supabase_user_client.verify_token(jwt)
        │    (signature + exp)  │  RS256 signature check + expiry validation
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────┐
        │ 3. Token revocation   │  Redis lookup: security:revoked-token:{hash}
        │    check              │  Also checks user-wide: security:revoked-admin:{user_id}
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────┐
        │ 4. Master DB lookup   │  SELECT User WHERE supabase_user_id = ...
        │    (short-lived conn) │  Connection released immediately after query
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────┐
        │ 5. Status validation  │  user.status == ACTIVE
        │                       │  user.admin_status == APPROVED
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────┐
        │ 6. Build UserIDs      │  { user_id, organisation_id, user_name,
        │                       │    user_email, user_timezone }
        └───────────────────────┘
```

**Key files:**
- `src/infra/auth/user_auth.py` — Token extraction and Supabase verification
- `src/api/dependencies/organisation_dependencies.py` — `get_current_user()` dependency
- `src/infra/auth/supabase_user_client.py` — Supabase SDK wrapper
- `src/infra/security/token_revocation.py` — Redis-backed revocation

### 3.2 Token Revocation

Tokens can be revoked at two levels:
- **Single token**: On logout, `security:revoked-token:{hash(jwt)}` is set in Redis with TTL matching the token's remaining lifetime.
- **User-wide**: Admin lockout sets `security:revoked-admin:{user_id}` with configurable TTL (default 1 hour), blocking all sessions.

Policy: **Fail-open.** If Redis is unavailable, requests are allowed (with a warning log). This prevents a Redis outage from becoming a full authentication outage.

### 3.3 Rate Limiting

All voice endpoints use `rate_limit_user_write`:
- Fixed-window counters in Redis, keyed by `rl:u:{user_id}:cat:write:{time_bucket}`
- Default limits: 5 requests/minute, 5 requests/hour (configurable)
- Returns HTTP 429 when exceeded
- Fail-open on Redis unavailability

### 3.4 Ephemeral Key Isolation

The backend's Azure OpenAI API key never reaches the mobile app. Instead:
1. Backend authenticates the user via JWT
2. Backend calls Azure's `/openai/v1/realtime/client_secrets` endpoint using its own API key
3. Azure returns a short-lived ephemeral key (1-hour TTL)
4. Mobile receives only the ephemeral key and uses it for the WebRTC connection
5. The ephemeral key grants access only to the specific realtime session — it cannot be used for other Azure API calls

This means: even if a mobile device is compromised, the attacker gets a single-use, time-limited key — not the backend's API key.

---

## 4. Session Bootstrap Flow

When the user taps "Talk to Kai", the following sequence executes:

```
Mobile App                          Backend API                        Azure OpenAI
    │                                   │                                  │
    │  POST /users/realtime/session     │                                  │
    │  {device, conversation_mode}      │                                  │
    │──────────────────────────────────►│                                  │
    │                                   │                                  │
    │                                   │  Validate JWT (auth chain)       │
    │                                   │  Check rate limit                │
    │                                   │                                  │
    │                                   │  POST /openai/v1/realtime/       │
    │                                   │       client_secrets             │
    │                                   │  {session: {type, model, voice}} │
    │                                   │─────────────────────────────────►│
    │                                   │                                  │
    │                                   │  {value: ephemeral_key,          │
    │                                   │   expires_at, session.id}        │
    │                                   │◄─────────────────────────────────│
    │                                   │                                  │
    │                                   │  Create ChatSession              │
    │                                   │  (chat_type=VOICE_CHAT)          │
    │                                   │  in org database                 │
    │                                   │                                  │
    │  {session_id, ephemeral_key,      │                                  │
    │   endpoint, model, voice,         │                                  │
    │   expires_at, turn_mode,          │                                  │
    │   chat_session_id, runtime_config}│                                  │
    │◄──────────────────────────────────│                                  │
    │                                   │                                  │
```

**Request** (`POST /users/realtime/session`):
```json
{
  "device": {
    "platform": "android",
    "app_version": "1.2.3"
  },
  "conversation_mode": "push_to_talk",
  "chat_session_id": null
}
```

If `chat_session_id` is provided, the backend links the new Azure session to an existing `ChatSession` (session resume). If null, a new `ChatSession` is created.

**Response**:
```json
{
  "session_id": "azure-session-abc123",
  "ephemeral_key": "ek_...",
  "endpoint": "https://<deployment>.openai.azure.com/openai/v1/realtime/calls",
  "model": "gpt-realtime-mini",
  "voice": "alloy",
  "expires_at": "2026-04-13T13:00:00Z",
  "turn_mode": "push_to_talk",
  "chat_session_id": "550e8400-e29b-41d4-a716-446655440000",
  "runtime_config": {
    "chat_session_id": "550e8400-e29b-41d4-a716-446655440000",
    "control_mode": "backend_sideband",
    "tool_policy": "guardrailed_auto",
    "allowed_tools": ["search_internal_knowledge", "get_deal_details"],
    "session_update": {
      "type": "session.update",
      "session": {
        "modalities": ["text", "audio"],
        "instructions": "You are Kai, CopilotGTM's AI sales assistant...",
        "tools": [
          {
            "type": "function",
            "name": "search_internal_knowledge"
          }
        ],
        "audio": {
          "input": {
            "format": {"type": "audio/pcm", "sample_rate_hz": 16000},
            "transcription": {"model": "whisper-1"}
          },
          "output": {
            "format": {"type": "audio/pcm", "sample_rate_hz": 24000},
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
```

### 4.1 Runtime Contract Ownership

`runtime_config` is the signed bridge between backend bootstrap and backend sideband control:

- `control_mode=backend_sideband` tells the frontend that the backend owns prompt and tool control for the live call.
- `session_update` contains the backend-authored voice instructions, audio settings, and realtime `session.tools`.
- `allowed_tools` is signed policy metadata that tells the backend which tool profile was approved.
- `tool_policy` communicates that grounded answers may require backend tool usage.
- `signature` lets the backend detect tampering when the runtime contract is later audited or
  validated.

The frontend must not:

- generate its own prompt,
- rewrite the `session.update` payload,
- define its own realtime tool schemas,
- execute CRM, knowledge, or organisation-scoped tools in the client.

Tools belong on the backend because they require tenant context, database access, and secrets.

**Key files:**
- `src/api/routers/users/realtime_handler.py` — Endpoint definition
- `src/service/users/realtime_session_service.py` — Bootstrap logic + ChatSession creation
- `src/domain/models/user_models.py` — Request/response schemas

---

## 5. WebRTC Connection

After receiving the bootstrap response, the mobile app establishes a direct WebRTC connection to Azure. The backend is not involved in this step.

### 5.1 SDP Exchange

```
Mobile App                                         Azure OpenAI
    │                                                  │
    │  1. Create RTCPeerConnection                     │
    │  2. Create RTCDataChannel("oai-events")          │
    │  3. getUserMedia({audio: true})                   │
    │  4. Add audio tracks (disabled)                   │
    │  5. Create SDP offer                             │
    │                                                  │
    │  POST {endpoint}?webrtcfilter=on                 │
    │  Authorization: Bearer {ephemeral_key}           │
    │  Content-Type: application/sdp                   │
    │  Body: <SDP offer>                               │
    │─────────────────────────────────────────────────►│
    │                                                  │
    │  200 OK                                          │
    │  Content-Type: application/sdp                   │
    │  Body: <SDP answer>                              │
    │◄─────────────────────────────────────────────────│
    │                                                  │
    │  6. Set remote description (SDP answer)          │
    │  7. ICE candidates exchange (STUN/TURN)          │
    │  8. Data channel opens                           │
    │  9. Read live call_id from realtime session      │
    │                                                  │
    │  POST /users/realtime/calls/attach               │
    │  {session_id, call_id, chat_session_id,          │
    │   runtime_config}                                │
    │──────────────────────────────────► Backend       │
    │                                                  │
    │                             Backend opens sideband websocket
    │                             and sends session.update + tools
    │                                                  │
```

### 5.2 Audio Track Management

Push-to-talk is implemented by enabling/disabling WebRTC audio tracks:

| Action | Audio Tracks | Data Channel Message |
|--------|-------------|---------------------|
| User taps "Talk" | Enabled | `input_audio_buffer.clear` |
| User releases | Disabled | `input_audio_buffer.commit` + `response.create` |
| Disconnect | Removed | Peer connection closed |

Audio flows as PCM16 over the WebRTC media track. Control messages flow over the `oai-events` data channel as JSON.

### 5.3 Stale Generation Tracking

`AzureRealtimeClient` maintains a `connectGeneration` counter. Each `connect()` call increments it. If the user triggers `connect()` while a previous connection is still setting up, the earlier attempt detects the generation mismatch and aborts. This prevents race conditions from rapid connect/disconnect cycles.

**Key file:** `apps/mobile/src/services/realtime/azure-realtime-client.ts`

---

## 6. Voice Conversation Loop

### 6.1 XState State Machine

The voice session state machine enforces valid transitions and prevents impossible states:

```
                    BOOTSTRAP_READY
            ┌───────────────────────────┐
            ▼                           │
         ┌──────┐    CONNECT     ┌────────────┐
         │ idle │───────────────►│ connecting  │
         └──────┘                └─────┬──────┘
            ▲                          │
            │ BOOTSTRAP_CLEARED        │ CONNECTED
            │                          ▼
         ┌──────┐               ┌─────────┐
         │failed│◄──────────────│  ready   │◄──────────────────────┐
         └──────┘  CONN_FAILED  └────┬─────┘                       │
            │                        │                             │
            │ CONNECT (retry)        │ START_RECORDING             │
            └────────────────►       ▼                             │
                              ┌───────────┐                        │
                              │ recording  │                        │
                              └─────┬─────┘                        │
                                    │                              │
                                    │ STOP_RECORDING               │
                                    ▼                              │
                              ┌────────────┐                       │
                              │ committing  │                       │
                              └─────┬──────┘                       │
                                    │                              │
                                    │ AUDIO_COMMITTED              │
                                    ▼                              │
                            ┌──────────────────┐                   │
                            │ waiting_for_model │                   │
                            └────────┬─────────┘                   │
                                     │                             │
                                     │ MODEL_AUDIO_STARTED         │
                                     ▼                             │
                            ┌──────────────────┐                   │
                            │ playing_response  │───────────────────┘
                            └──────────────────┘  MODEL_AUDIO_FINISHED
```

All states handle `DISCONNECT` gracefully, transitioning back to `idle`.

**Key file:** `apps/mobile/src/features/voice-session/machine/voice-session-machine.ts`

### 6.2 Azure Server Events

During a conversation turn, Azure sends events over the data channel. The controller normalizes variant event names and updates state:

| Azure Event | What Happens |
|------------|--------------|
| `conversation.item.input_audio_transcription.completed` | User's speech transcribed — added to transcript as user entry |
| `response.created` | Azure starts generating a response |
| `response.audio_transcript.delta` | Streaming text of assistant's spoken response — updates draft |
| `response.audio.delta` | Audio chunk streaming to speaker |
| `response.audio_transcript.done` | Assistant response complete — finalized in transcript |
| `response.audio.done` | Audio playback complete — XState transitions to `ready` |
| `error` | Error event — XState transitions to `failed` |

**Event normalization:** Azure sometimes emits `response.output_audio_transcript.delta` instead of `response.audio_transcript.delta` depending on the pipeline. The controller maps these to canonical names.

### 6.3 Transcript Accumulation

`useVoiceSessionController` maintains a `transcriptEntries` array in React state:

```typescript
type TranscriptEntry = {
  id: string;           // Azure's item_id
  speaker: 'user' | 'assistant';
  text: string;
  timestamp: number;
};
```

Entries accumulate in order. Assistant entries start as drafts (updated on each `delta` event) and finalize on `done`. This array is the source of truth for what gets persisted.

**Key file:** `apps/mobile/src/features/voice-session/hooks/useVoiceSessionController.ts`

---

## 7. Turn Persistence

Voice turns are persisted as `ChatMessage` rows so they appear in chat history and can be resumed later.

### 7.1 Persistence Endpoint

**`POST /users/chats/voice/turns`**

Request:
```json
{
  "chat_session_id": "550e8400-e29b-41d4-a716-446655440000",
  "turns": [
    {
      "realtime_item_id": "item_abc123",
      "speaker": "user",
      "text": "How is the Acme deal looking?",
      "turn_index": 0
    },
    {
      "realtime_item_id": "item_def456",
      "speaker": "assistant",
      "text": "The Acme deal is in the negotiation stage...",
      "turn_index": 1
    }
  ]
}
```

Response:
```json
{
  "status": "success",
  "data": {
    "persisted_count": 2,
    "message_ids": ["uuid-1", "uuid-2"]
  }
}
```

### 7.2 Persistence Logic

1. **Ownership check** — Verify `chat_session_id` belongs to the authenticated user's org and is `chat_type=VOICE_CHAT`
2. **Deduplication** — Query existing messages for this session, extract `content->>'realtime_item_id'` values. Skip turns whose `realtime_item_id` already exists.
3. **Create ChatMessages** — For each new turn:
   ```python
   ChatMessage(
       session_id=chat_session_id,
       role=USER if speaker == "user" else ASSISTANT,
       content={
           "text": "transcribed text",
           "source": "voice_transcription",
           "realtime_item_id": "item_abc123"
       },
       extra={"turn_index": 0}
   )
   ```
4. **Auto-title** — If the session title is still "Voice Session" and this batch contains a user turn, set the title to the first 60 characters of the user's text.
5. **Commit** — Single transaction for all turns in the batch.

### 7.3 Idempotency

The `realtime_item_id` field (Azure's unique identifier for each conversation item) serves as the dedup key. Calling the endpoint multiple times with the same turns produces the same result — already-persisted turns are skipped, and `persisted_count` reflects only newly created messages.

**Key file:** `src/api/routers/chat/voice_realtime_handler.py`

---

## 8. Multi-Tenant Data Model

### 8.1 Database Architecture

CopilotGTM is multi-tenant with per-organization database isolation:

```
                  ┌─────────────────────┐
                  │  Master Database    │
                  │  (shared)           │
                  │                     │
                  │  - users            │
                  │  - organisations    │
                  │  - supabase_ids     │
                  └─────────┬───────────┘
                            │
                     ┌──────┴──────┐
                     │  pgbouncer  │
                     │  (role-based│
                     │   routing)  │
                     └──────┬──────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │ Org A DB     │ │ Org B DB     │ │ Org C DB     │
    │              │ │              │ │              │
    │ chat_sessions│ │ chat_sessions│ │ chat_sessions│
    │ chat_messages│ │ chat_messages│ │ chat_messages│
    │ deals, etc.  │ │ deals, etc.  │ │ deals, etc.  │
    └──────────────┘ └──────────────┘ └──────────────┘
```

- **Master DB** — User authentication data, organization metadata. Short-lived connections (released after user lookup).
- **Org DBs** — All business data. Accessed via `get_org_async_session(organisation_id)`. pgbouncer routes by PostgreSQL role (`org_{org_id}`).
- **Connection pooling** — SQLAlchemy QueuePool (app-side, `pool_size=10`, `max_overflow=20`) → pgbouncer → PostgreSQL.

### 8.2 ChatSession Schema

```
┌─────────────────────────────────────────────────────────┐
│ chat_sessions                                            │
├─────────────────────┬───────────────────────────────────┤
│ session_id          │ UUID (PK, auto-generated)         │
│ user_id             │ UUID (FK to users)                │
│ organisation_id     │ UUID (tenant key)                 │
│ title               │ String — "Voice Session" initially │
│ chat_type           │ String(50) — "VOICE_CHAT"         │
│ chat_version        │ String — "v1"                     │
│ properties          │ JSONB:                            │
│                     │   realtime_session_id: string     │
│                     │   device_platform: string         │
│                     │   model: string                   │
│                     │   voice: string                   │
│ tags                │ ARRAY[String]                     │
│ is_deleted          │ Boolean (soft delete)             │
│ is_favorite         │ Boolean                           │
│ created_at          │ DateTime (auto)                   │
│ updated_at          │ DateTime (auto)                   │
├─────────────────────┴───────────────────────────────────┤
│ INDEX: ix_chat_sessions_organisation_id_user_id          │
└─────────────────────────────────────────────────────────┘
```

### 8.3 ChatMessage Schema

```
┌─────────────────────────────────────────────────────────┐
│ chat_messages                                            │
├─────────────────────┬───────────────────────────────────┤
│ message_id          │ UUID (PK, auto-generated)         │
│ session_id          │ UUID (FK to chat_sessions)        │
│ parent_message_id   │ UUID (nullable, for threads)      │
│ role                │ String — "user" | "assistant"     │
│ message_type        │ String — "GENERAL_MESSAGE"        │
│ message_version     │ Integer — 1                       │
│ content             │ JSONB:                            │
│                     │   text: string (transcript)       │
│                     │   source: "voice_transcription"   │
│                     │   realtime_item_id: string (dedup)│
│ extra               │ JSONB:                            │
│                     │   turn_index: integer             │
│ model_name          │ String (nullable)                 │
│ feedback_score      │ Integer (nullable)                │
│ is_edited           │ Boolean                           │
│ agent_data          │ JSONB (tool calls, etc.)          │
│ created_at          │ DateTime (auto)                   │
│ updated_at          │ DateTime (auto)                   │
└─────────────────────────────────────────────────────────┘
```

### 8.4 ChatType Enum

`ChatType` is stored as `String(50)` (not a Postgres ENUM), so adding new types requires no Alembic migration:

```python
class ChatType(str, Enum):
    GENERAL_CHAT = "GENERAL_CHAT"
    TEAMS_CHAT = "TEAMS_CHAT"
    SLACK_CHAT = "SLACK_CHAT"
    TECHNICAL_DECK = "TECHNICAL_DECK"
    RFP_SHEET = "RFP_SHEET"
    VOICE_CHAT = "VOICE_CHAT"
```

---

## 9. Voice Agent (Kai)

### 9.1 Architecture

The live Azure session is controlled by a backend sideband runtime that sends `session.update`,
receives realtime function calls, and executes approved backend tools. `VoiceRealtimeAgent`
still defines the shared prompt/tool profile, but it is not the transport layer for the live
Azure session.

```
User speaks
    │
    ▼
Azure Realtime API
    │
    ├─► Speech-to-text (Whisper)
    │       │
    │       ▼
    │   Text prompt ──► VoiceRealtimeAgent.respond()
    │                       │
    │                       ├─► Tool calls (CRM, knowledge base, web search)
    │                       │
    │                       ▼
    │                   Text response
    │       │
    │       ▼
    ├─► Text-to-speech (voice: alloy)
    │
    ▼
User hears response
```

### 9.2 Agent Configuration

The voice agent shares the same foundation as the `GeneralAgent` used in web chat:

| Feature | GeneralAgent | VoiceRealtimeAgent |
|---------|-------------|-------------------|
| Model | Configured per agent | `gpt-realtime-mini` (via agent_config) |
| Tools | Full tool registry | Same tools (via `get_tools_for_agent`) |
| Skills | Agno LocalSkills from `src/agents/skills/` | Same skills directory |
| Datetime | Timezone-aware via `get_agno_datetime_settings` | Same |
| Markdown | Enabled | Disabled (`markdown=False`) |
| Prompt | Full prompt with AEP metadata, mermaid diagrams | Same core, but voice output constraints instead |

### 9.3 Voice Prompt Differences

The voice prompt is a complete standalone prompt (not a diff from the general prompt). It includes all the same sections — Core Principles, How to Work, Skills, Tool Selection, File Attachments, Sales Intelligence — but replaces the output format section with voice-specific constraints:

- Keep responses concise for spoken delivery (2-4 sentences for simple answers)
- No markdown formatting (no headers, bold, tables, bullet points)
- No metadata blocks (`AEP`, `AGENT_TASKS`)
- Structure complex answers conversationally ("First... Second... Finally...")
- Limit lists to top 3-5 items
- Ask one short follow-up when ambiguous

**Key files:**
- `src/agents/workflows/voice_realtime/agent.py` — Agent construction
- `src/agents/workflows/voice_realtime/prompts.py` — System prompt template
- `src/infra/config/agent_config.py` — Model/voice/timeout configuration

---

## 10. API Reference

### Voice Realtime Endpoints

| Method | Path | Auth | Rate Limit | Purpose |
|--------|------|------|-----------|---------|
| POST | `/users/realtime/session` | JWT (Bearer) | `rate_limit_user_write` | Bootstrap Azure ephemeral key + create ChatSession |
| POST | `/users/realtime/calls/attach` | JWT (Bearer) | `rate_limit_user_write` | Attach backend sideband control to the live realtime call |
| POST | `/users/chats/voice/turns` | JWT (Bearer) | `rate_limit_user_write` | Persist completed voice turns as ChatMessages |

### Supporting Endpoints (existing, used by voice)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/users/chats/list_chats?chat_type=VOICE_CHAT` | JWT | List voice sessions (filterable by chat_type) |
| GET | `/users/chats/messages/list?session_id=...` | JWT | Read transcript messages for a session |
| POST | `/users/auth/refresh-token` | Refresh token | Refresh expired access token |
| GET | `/users/auth/me` | JWT | Get current user profile |

### Request/Response Schemas

**UserRealtimeSessionRequest:**
```
device.platform:      "android" | "ios" | "web"
device.app_version:   string (e.g., "1.2.3")
conversation_mode:    "push_to_talk"
chat_session_id:      string | null (UUID, for session resume)
```

**UserRealtimeSessionResponse:**
```
session_id:           string (Azure session ID)
ephemeral_key:        string (1-hour JWT for WebRTC)
endpoint:             string (Azure WebRTC URL)
model:                string (e.g., "gpt-realtime-mini")
voice:                string (e.g., "alloy")
expires_at:           datetime (UTC)
turn_mode:            "push_to_talk"
chat_session_id:      string (UUID — backend ChatSession ID)
```

**PersistVoiceTurnsRequest:**
```
chat_session_id:      string (UUID)
turns[].realtime_item_id:  string (Azure item ID, dedup key)
turns[].speaker:           "user" | "assistant"
turns[].text:              string (transcribed text)
turns[].turn_index:        integer (ordinal position)
```

**PersistVoiceTurnsResponse:**
```
persisted_count:      integer
message_ids:          string[] (UUIDs of created ChatMessages)
```

---

## 11. Error Handling and Resilience

### 11.1 Backend Error Handling

| Scenario | Status Code | Behavior |
|----------|------------|----------|
| Azure config missing | 500 | Logged with details, user sees generic error |
| Azure API error | 502 | Upstream status code logged, returns "service temporarily unavailable" |
| Azure timeout | 503 | Duration captured, returns "service temporarily unavailable" |
| Azure response malformed | 502 | Payload details logged for debugging |
| Session not found (turns) | 404 | "Voice chat session not found" |
| DB error (turns) | 500 | Transaction rolled back, generic error returned |
| Rate limit exceeded | 429 | "Too many requests" with retry guidance |
| Token revoked | 401 | "Unauthorized" |

### 11.2 Frontend Error Handling

**XState machine guarantees:**
- Invalid transitions are impossible (e.g., cannot start recording while connecting)
- `failed` state preserves error message for display
- `DISCONNECT` is handled from every state
- Retry from `failed` is allowed if bootstrap data exists

**Connection resilience:**
- `connectGeneration` counter prevents stale connection callbacks
- `connectInFlightRef` prevents concurrent connection attempts
- Audio tracks are always disabled on disconnect (prevents microphone leak)
- Peer connection and data channel are fully torn down on disconnect

**User feedback:**
- Error messages surface through `viewModel.errorMessage`
- Primary action button shows "Try Again" in failed state
- No automatic retry — waits for explicit user action

### 11.3 Redis Fail-Open

Both rate limiting and token revocation use a fail-open policy: if Redis is unavailable, the request proceeds (with a warning log). Rationale: a Redis outage should degrade security monitoring, not cause a full service outage. In practice, Redis is highly available behind Azure Cache for Redis with zone redundancy.

### 11.4 Database Session Management

Org database sessions are acquired via async context managers that guarantee connection release:
- `get_org_async_session()` yields an `AsyncSession` and releases it when the request completes
- Master DB connections are acquired and released within the auth chain (not held for the request lifetime)
- pgbouncer handles backend connection reuse across requests
- `pool_pre_ping=True` detects and replaces stale connections

---

## 12. Complete Data Flow Summary

```
┌─ USER TAPS "TALK TO KAI" ─────────────────────────────────────────────┐
│                                                                        │
│  1. Mobile: POST /users/realtime/session (JWT in header)               │
│  2. Backend: Validate JWT → Supabase → Master DB → UserIDs            │
│  3. Backend: POST Azure /client_secrets → ephemeral_key               │
│  4. Backend: Create ChatSession(VOICE_CHAT) in org DB                  │
│  5. Mobile: Receives {ephemeral_key, endpoint, chat_session_id}       │
│                                                                        │
├─ WEBRTC CONNECT ──────────────────────────────────────────────────────┤
│                                                                        │
│  6. Mobile: Create RTCPeerConnection + DataChannel                     │
│  7. Mobile: getUserMedia → add audio tracks (disabled)                 │
│  8. Mobile: SDP offer → POST Azure endpoint → SDP answer              │
│  9. Mobile: receive live call_id from Azure session                    │
│ 10. Mobile: POST /users/realtime/calls/attach                          │
│ 11. Backend: open sideband websocket + send session.update + tools     │
│ 12. XState: idle → connecting → ready                                  │
│                                                                        │
├─ VOICE CONVERSATION (repeats per turn) ───────────────────────────────┤
│                                                                        │
│ 13. User holds talk → audio tracks enabled → PCM16 streams to Azure   │
│ 14. User releases → tracks disabled → commit + response.create         │
│ 15. Azure: Whisper transcription → input_audio_transcription.completed │
│ 16. Azure: model may emit realtime function call event                 │
│ 17. Backend: execute approved tool → function_call_output → response   │
│ 18. Azure: Response complete → audio_transcript.done                   │
│ 19. Mobile: transcript entries updated in React state                  │
│ 20. XState: ready → recording → committing → waiting → playing → ready│
│                                                                        │
├─ TURN PERSISTENCE ────────────────────────────────────────────────────┤
│                                                                        │
│ 21. Mobile: POST /users/chats/voice/turns {chat_session_id, turns}    │
│ 22. Backend: Verify session ownership + VOICE_CHAT type               │
│ 23. Backend: Dedup by realtime_item_id                                 │
│ 24. Backend: Create ChatMessage rows, auto-title session               │
│ 25. Backend: Commit transaction                                        │
│                                                                        │
├─ SESSION END ─────────────────────────────────────────────────────────┤
│                                                                        │
│ 23. User taps disconnect or navigates away                             │
│ 24. Mobile: Flush remaining unpersisted turns                          │
│ 25. Mobile: Close peer connection, release audio                       │
│ 26. XState: → idle                                                     │
│                                                                        │
├─ LATER: VIEW HISTORY ─────────────────────────────────────────────────┤
│                                                                        │
│ 27. Mobile: GET /users/chats/list_chats?chat_type=VOICE_CHAT          │
│ 28. Mobile: GET /users/chats/messages/list?session_id=...              │
│ 29. Display transcript as chat bubbles                                 │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```
