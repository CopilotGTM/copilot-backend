# Bots API Documentation

## Base URL

```text
/users/bots
```

These endpoints are used by chat platforms and the bot-auth frontend flow. Platform webhook
endpoints authenticate with provider-specific request verification, not the normal user bearer
token.

---

## 1) Validate Bot Auth State

```http
GET /users/bots/auth/validate-state?state=<state_token>
```

Checks whether a bot sign-in state token is still valid without consuming it.

## 2) Complete Bot Auth Callback

```http
POST /users/bots/auth/callback
Content-Type: application/json

{
  "access_token": "<supabase_access_token>",
  "state": "<state_token>"
}
```

Links the external bot identity encoded in the state token to the signed-in CopilotGTM user.
Supported chat-bot providers include `teams`, `slack`, `whatsapp`, and `google_chat`.

---

## 3) Google Chat Interaction Events

```http
POST /users/bots/google-chat/events
Authorization: Bearer <google_chat_token>
Content-Type: application/json
```

Receives Google Chat app interaction events. The backend verifies Google Chat's bearer token
against `GOOGLE_CHAT_AUTH_AUDIENCE`; invalid or missing tokens return `401`.

### Supported Events

| Event | Behavior |
|-------|----------|
| `ADDED_TO_SPACE` | Returns a synchronous welcome message. |
| `MESSAGE` | Returns a sign-in message for unlinked users, or creates a bot job and returns `Thinking...` for linked users. |
| `REMOVED_FROM_SPACE` | Acknowledges removal with an empty `200` response. |
| `CARD_CLICKED`, `SUBMIT_FORM`, `APP_COMMAND` | Acknowledges with a safe unsupported-action message in v1. |

### Runtime Notes

- Duplicate message/action events are deduped using Redis for 300 seconds.
- Linked `MESSAGE` events enqueue `chat.process_bot_message` with provider `google_chat`.
- Final async replies use Google Chat `spaces.messages.create` with app authentication from
  Application Default Credentials or Workload Identity Federation and the
  `https://www.googleapis.com/auth/chat.bot` scope.
- If the inbound event includes a thread, the async reply preserves it with
  `REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD`.

### Credential Model

Google Chat is keyless. Do not create or read a service-account JSON key secret for outbound
messages. Local development uses Google Application Default Credentials with service-account
impersonation. Dev and production should use Workload Identity Federation with
`GOOGLE_APPLICATION_CREDENTIALS` pointing at the WIF credential config file.

For local ngrok testing, set `GOOGLE_CHAT_AUTH_AUDIENCE` to the exact Google Chat
authentication audience URL. If unset, it derives from `APP_URL`.
