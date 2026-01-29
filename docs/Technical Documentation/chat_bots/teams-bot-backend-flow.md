# Microsoft Teams Bot Backend Flow (CopilotGTM)

## Overview

This document explains **how the Teams bot is accessed**, **what functionality it supports**,
and **how it connects to the agent pipeline** in the CopilotGTM backend. It is focused on the
current implementation in this repository and is meant to be a durable reference for engineers
and non-engineers alike.

### High-level Flow

```
Teams Client
   ↓ (message)
Azure Bot Service
   ↓ POST /bots/teams/messages
FastAPI Webhook (teams_webhook_handler.py)
   ↓ TeamsBotService.process_activity()
TeamsBotService (identity + commands)
   ↓ enqueue chat.process_bot_message
Celery chat_worker task
   ↓ BotMessageProcessingService → GeneralAgent
BotMessageProcessingService
   ↓ proactive reply via TeamsBotService
Teams Client
```

---

## Entry Points & Routing

### Router Wiring

- **Router package**: `src/api/routers/bots/__init__.py`
- **Teams router**: `src/api/routers/bots/teams_webhook_handler.py`

Routes are mounted under `/bots`:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/bots/teams/messages` | POST | Teams webhook endpoint (Bot Framework activities) |
| `/bots/teams/health` | GET | Health check |

> The Teams-specific webhook handler is registered in `bots/__init__.py` using:
> `from .teams_webhook_handler import router as teams_router` and `include_router(..., prefix="/teams")`.

---

## Teams Webhook Handler

**File:** `src/api/routers/bots/teams_webhook_handler.py`

Responsibilities:

1. **Feature flag check** (`TEAMS_ENABLED`)
2. **Parse activity payload** from Bot Framework
3. **Log incoming activity metadata**
4. **Delegate processing** to `TeamsBotService.process_activity()`
5. **Always return 200 OK** to prevent Azure Bot Service retries

Key notes:

- No standard auth dependency is used; Bot Framework JWT auth is expected.
- The Authorization header is passed to the service for validation (SDK-based).
- Errors are logged but still return HTTP 200 to avoid message retry storms.

---

## TeamsBotService (Core Bot Logic)

**File:** `src/service/bots/teams_bot_service.py`

### Responsibilities

- Load bot credentials from **Azure Key Vault** on first use
- Process **activity types**: message, conversationUpdate, invoke
- Handle **identity linking** with external identities
- Route commands (`help`, `status`, `login`, `logout`)
- Send responses back to Teams (direct or proactive)

### Commands Supported

| Command | Behavior |
|---------|----------|
| `help` | Returns command list and instructions |
| `status` | Returns signed-in status for the linked CopilotGTM user |
| `login` | Generates login URL with state token |
| `logout` | Deactivates the external identity mapping |

### Identity Linking

Teams users are mapped to CopilotGTM users via `ExternalIdentityService`:

- **Provider:** `ExternalIdentityProvider.TEAMS`
- Lookup by `(teams_user_id, teams_tenant_id)`
- If mapped: authenticated chat flows are enabled
- If not mapped: user is prompted to sign in

This mapping is stored in **master schema** via `external_identity_mappings`.

### Auth State Tokens

For login flows, the bot uses `BotAuthStateService`:

- Stores short-lived state tokens in Redis (5 min TTL)
- Generates login URL using `TEAMS_AUTH_LOGIN_URL`
- On callback, the frontend/backend validates & consumes state

---

## Message Handling (Authenticated Users)

### 1) Receive message
`TeamsBotService._handle_message()` captures:

- text
- user identifiers
- tenant ID
- service URL + conversation ID (for proactive replies)

### 2) Route message

`_route_message()` decides:

- Commands → immediate reply
- Non-command + authenticated → enqueue agent task
- Non-command + unauthenticated → login prompt

### 3) Enqueue Celery task

Authenticated messages call `_handle_authenticated_message()`:

```python
send_chat_task_async(
    "chat.process_bot_message",
    args=[
        str(user.organisation_id),
        str(user.user_id),
        text,
        "teams",
        {"service_url": service_url, "conversation_id": conversation_id},
    ],
    queue="chat_messages",
)
```

This produces an **immediate acknowledgement**: “Let me look into that for you...”.

---

## Agent Execution (Chat Worker)

### Task Entry Point

**File:** `src/workers/chat_worker/tasks/bot_message_processing.py`

- Celery task name: `chat.process_bot_message`
- Instantiates `BotMessageProcessingService`
- Delegates work synchronously from a Celery context

### BotMessageProcessingService

**File:** `src/workers/chat_worker/services/bot_message_processing_service.py`

Responsibilities:

1. Build a message list
2. Run the **GeneralAgent** with `run_agent_for_chat()`
3. Accumulate streaming frames into one final reply
4. Send the reply back to Teams via proactive message API

The agent is executed with a **fresh session pattern** (no DB sessions held during LLM calls).

### Agent Invocation

```python
async for frame in run_agent_for_chat(
    chat_type="GENERAL_CHAT",
    messages=messages,
    context=AgentContext(
        organisation_id=self.organisation_id,
        org_name="CopilotGTM",
        user_id=user_id,
    ),
):
    self._accumulate_frame(...)
```

---

## Reply Delivery (Proactive Messaging)

Replies are sent via the Bot Framework REST API:

1. `TeamsBotService._get_access_token()` fetches a token from Azure AD
2. `TeamsBotService._send_reply()` posts to:

```
{service_url}/v3/conversations/{conversation_id}/activities
```

This pattern is used for both:

- Immediate acknowledgements (webhook path)
- Agent responses (chat worker path)

---

## Configuration & Secrets

**Config file:** `src/infra/config/configs.py` (Teams fields)

Required settings:

| Config | Purpose |
|--------|---------|
| `TEAMS_ENABLED` | Feature flag to enable the bot |
| `TEAMS_AUTH_LOGIN_URL` | Login page for identity linking |
| `SECRET_NAME_BOT_APP_ID` | Key Vault secret name |
| `SECRET_NAME_BOT_APP_SECRET` | Key Vault secret name |
| `SECRET_NAME_BOT_TENANT_ID` | Key Vault secret name |

**Secrets are fetched at runtime** from the Master Key Vault via
`TeamsBotService._get_master_secret()`.

---

## Error Handling & Observability

- Webhook handler logs request metadata and error details.
- Bot processing exceptions are logged, but 200 OK is returned to Azure Bot Service.
- Chat worker logs successes and failures for each task.

---

## Extensibility & Future Work

Where to extend:

- **New commands:** add to `_route_message()` in `TeamsBotService`
- **New bot providers:** extend `BotMessageProcessingService._send_reply()`
- **Rich cards / adaptive cards:** implement in `_handle_invoke()`

---

## Quick Reference

### Key Files

| Layer | File |
|-------|------|
| Router | `src/api/routers/bots/teams_webhook_handler.py` |
| Service | `src/service/bots/teams_bot_service.py` |
| Auth State | `src/service/bots/auth_state_service.py` |
| Celery Task | `src/workers/chat_worker/tasks/bot_message_processing.py` |
| Agent Service | `src/workers/chat_worker/services/bot_message_processing_service.py` |

### Activity Types Supported

- `message`
- `conversationUpdate`
- `invoke` (logged only)

---

## Summary

The Teams bot in CopilotGTM is implemented as a **webhook-driven entrypoint** that routes
messages through a **bot service**, uses **identity mapping** for secure access, and leverages
the existing **GeneralAgent + chat worker** pipeline to generate responses. Responses are sent
back to Teams using **proactive messaging** via the Bot Framework API.

This document should remain accurate as long as:
- the webhook remains in `src/api/routers/bots/teams_webhook_handler.py`
- `TeamsBotService` and the chat worker tasks remain the processing backbone