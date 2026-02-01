# Teams Bot Chat Architecture

## Overview

The Teams bot handles two distinct conversation types — **personal chat** and **group chat** — each with a different strategy for message history. This document explains how each flow works end-to-end, what data is stored where, and how the web UI is kept separate.

---

## 1. Personal Chat (1:1 with the Bot)

### How It Works

When a user sends a direct message to the CopilotGTM bot in Microsoft Teams, the message flows through our own webhook. We persist every message (both user and assistant) in our database, and load history from DB when processing the next message.

### End-to-End Flow

```
User sends message in Teams
        │
        ▼
Azure Bot Service delivers activity to our webhook
  POST /api/bots/teams/webhook
        │
        ▼
TeamsBotService._handle_message()
  ├── Looks up linked CopilotGTM user via external_identity_mappings
  ├── Detects conversation_type = "personal"
  └── Enqueues Celery task: chat.process_bot_message
        │
        ▼
BotMessageProcessingService.process()  [Celery worker]
  ├── 1. _get_or_create_teams_session(user_id)
  │       → Finds or creates a ChatSession with chat_type=TEAMS_CHAT
  │       → One session per user per org, reused forever
  │
  ├── 2. _persist_user_message(session_id, message_text)
  │       → Saves user message to chat_messages table
  │       → Returns message_id
  │
  ├── 3. _load_history(session_id, limit=20)
  │       → Loads last 20 messages from DB (chronological order)
  │       → Strips the just-persisted user message (to avoid duplication)
  │       → Returns [{"role": "user"/"assistant", "content": "..."}]
  │
  ├── 4. _run_agent_and_accumulate()
  │       → Builds message list: history + current message
  │       → Runs GeneralAgent (no DB session held during LLM call)
  │       → Accumulates response frames
  │
  ├── 5. _persist_assistant_message(session_id, response_text, user_msg_id)
  │       → Saves assistant response to chat_messages table
  │       → Links to user message via parent_message_id
  │
  └── 6. Sends reply to Teams via proactive messaging API
```

### Database Tables (org_* schema)

**chat_sessions**

| Column            | Value                          |
|-------------------|--------------------------------|
| session_id        | UUID (auto-generated)          |
| user_id           | Linked CopilotGTM user ID     |
| organisation_id   | User's org ID                  |
| chat_type         | `TEAMS_CHAT`                   |
| title             | `"Teams Chat"`                 |
| is_deleted        | false                          |

- One row per user per org. Created on first message, reused for all subsequent messages.

**chat_messages**

| Column            | Value                                      |
|-------------------|--------------------------------------------|
| message_id        | UUID (auto-generated)                      |
| session_id        | FK to chat_sessions                        |
| role              | `user` or `assistant`                      |
| message_type      | `GENERAL_MESSAGE`                          |
| content           | `{"text": "the message text"}`             |
| parent_message_id | For assistant messages, points to the user message it replies to |

### Key Design Decisions

- **No DB migration needed**: `chat_type` is a `String(50)` column, not a PostgreSQL enum. Adding `TEAMS_CHAT` only required updating the Python `ChatType` enum.
- **One session per user**: Instead of creating a new session per conversation or per day, each user gets exactly one `TEAMS_CHAT` session. This mirrors how Teams personal chat works — one continuous conversation.
- **Race condition handling**: If two messages arrive simultaneously and both try to create the session, the second one catches `IntegrityError`, rolls back, and re-queries to find the session the first one created.
- **Fresh session pattern**: DB sessions are acquired, used, and released before the LLM call. No connection is held during agent execution.

### Why Not Graph API?

Previously, personal chat history was fetched from Microsoft Graph API at message time. This was replaced because:

1. **Extra permissions**: Required `TeamsAppInstallation.ReadForUser.All` Azure AD permission
2. **Data isolation bug**: Graph API chat matching could return the wrong chat
3. **Latency/reliability**: External API call on every message added latency and failure modes
4. **Redundant**: Every message already flows through our webhook — we have 100% of the data

### What Was Removed

- `resolve_personal_chat_id()` in `teams_graph_client.py` — resolved the 1:1 chat ID via Microsoft's installedApps endpoint
- `fetch_chat_messages()` in `teams_graph_client.py` — fetched personal chat messages from Graph API
- `graph_chat_id` resolution at login time in `auth_callback_handler.py`
- `graph_chat_id` field from `bot_context` dict in `teams_bot_service.py`
- On-the-fly `graph_chat_id` resolution fallback in `bot_message_processing_service.py`

---

## 2. Group Chat (@mention in a Group)

### How It Works

When the bot is @mentioned in a Teams group chat, it does **not** persist messages to the database. Instead, the agent is given a tool (`fetch_teams_chat_messages`) that calls Microsoft Graph API to read the group chat history on demand.

### End-to-End Flow

```
User @mentions bot in group chat
        │
        ▼
Azure Bot Service delivers activity to our webhook
  POST /api/bots/teams/webhook
        │
        ▼
TeamsBotService._handle_message()
  ├── Detects conversation_type = "groupChat"
  ├── Includes teams_conversation_id in bot_context
  └── Enqueues Celery task: chat.process_bot_message
        │
        ▼
BotMessageProcessingService.process()  [Celery worker]
  ├── conversation_type != "personal" → skips DB persistence
  ├── _build_messages() prepends a system prompt instructing the agent
  │   to call fetch_teams_chat_messages tool FIRST
  ├── _run_agent_and_accumulate()
  │       → Agent receives system prompt + the @mention text
  │       → Agent calls fetch_teams_chat_messages tool
  │       → Tool calls Graph API: GET /v1.0/chats/{id}/messages
  │       → Agent gets group chat context, formulates response
  └── Sends reply to Teams via proactive messaging API
```

### Key Files

| File | Purpose |
|------|---------|
| `src/agents/tools/communication/fetch_teams_chat_messages.py` | Agent tool that calls Graph API |
| `src/service/bots/teams_graph_client.py` | `get_graph_access_token()` — authenticates with Graph API |

### Why Graph API for Group Chat?

- Group chats involve multiple users across multiple orgs — we don't receive all messages through our webhook (only @mentions)
- Graph API `Chat.Read.All` permission is sufficient (no extra permissions needed)
- The tool is called by the agent only when needed, not on every message

---

## 3. Web UI Chat Isolation

### Problem

Personal Teams bot chat sessions (`TEAMS_CHAT`) are stored in the same `chat_sessions` table as web UI chats (`GENERAL_CHAT`, `TECHNICAL_DECK`, `RFP_SHEET`). They must not appear in the web frontend.

### Solution

`ChatSessionService` filters out `TEAMS_CHAT` in both listing and search:

```python
# In list_sessions() and search_sessions():
col(ChatSession.chat_type) != ChatType.TEAMS_CHAT
```

This ensures:
- `GET /list_chats` — does NOT return TEAMS_CHAT sessions
- `GET /search_chats` — does NOT return TEAMS_CHAT sessions in search results
- The TEAMS_CHAT session is only used internally by `BotMessageProcessingService`

### File

`src/service/chat/chat_session_service.py`

---

## File Reference

| File | Role |
|------|------|
| `src/domain/models/organisation_tables/chat_session_models.py` | `ChatType` enum with `TEAMS_CHAT` value |
| `src/domain/models/organisation_tables/chat_message_models.py` | `ChatMessage` model — stores individual messages |
| `src/agents/registry.py` | Maps `TEAMS_CHAT` → `GeneralAgent` |
| `src/workers/chat_worker/services/bot_message_processing_service.py` | Core service: persists messages, loads history, runs agent |
| `src/service/bots/teams_bot_service.py` | Receives Teams webhook, enqueues Celery task |
| `src/service/bots/teams_graph_client.py` | Graph API auth (`get_graph_access_token`) for group chat tool |
| `src/agents/tools/communication/fetch_teams_chat_messages.py` | Agent tool for group chat history (Graph API) |
| `src/service/chat/chat_session_service.py` | Web UI chat listing — filters out TEAMS_CHAT |
| `src/api/routers/bots/auth_callback_handler.py` | Login callback — creates identity mapping, sends welcome message |

---

## Testing Checklist

1. **Personal chat persistence**: Send a personal message to the bot. Verify `chat_sessions` has a row with `chat_type='TEAMS_CHAT'` and `chat_messages` has the user + assistant messages.
2. **History loading**: Send a second message. Check logs — previous messages should be loaded from DB.
3. **Web UI isolation**: Call `GET /list_chats` — TEAMS_CHAT sessions must not appear.
4. **Group chat**: @mention the bot in a group chat. Verify the `fetch_teams_chat_messages` tool is called and the response is correct.
5. **Reset during testing**: `DELETE FROM chat_sessions WHERE chat_type = 'TEAMS_CHAT'` to clear test data.
