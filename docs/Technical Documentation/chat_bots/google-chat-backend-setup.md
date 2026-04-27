# Google Chat Backend Setup

This document records the current Google Chat backend setup for the Kai bot.

## Local Google Chat App

| Field | Value |
|-------|-------|
| App name | Kai - LOCAL |
| GCP project | copilotgtm-local |
| App ID / project number | 376783393982 |
| Runtime endpoint | `POST https://caitlyn-nonarmigerous-uninstructively.ngrok-free.dev/users/bots/google-chat/events` |
| Authentication audience | `https://caitlyn-nonarmigerous-uninstructively.ngrok-free.dev/users/bots/google-chat/events` |
| Bot service account | `kai-chat-bot-local@copilotgtm-local.iam.gserviceaccount.com` |

## Credential Strategy

Google Chat is configured without service-account JSON keys.

- Do not create `google-chat-service-account-credentials`.
- Do not read Google Chat service-account credentials from Key Vault.
- Local development uses Google Application Default Credentials with service-account
  impersonation.
- Dev and production should use Workload Identity Federation with
  `GOOGLE_APPLICATION_CREDENTIALS` pointing to a WIF credential config file.

For local Docker Compose, the API and chat worker mount the host gcloud ADC directory at
`/home/appuser/.config/gcloud` and set:

```text
GOOGLE_APPLICATION_CREDENTIALS=/home/appuser/.config/gcloud/application_default_credentials.json
```

Before starting local containers, create host ADC with service-account impersonation:

```powershell
gcloud auth application-default login --impersonate-service-account=kai-chat-bot-local@copilotgtm-local.iam.gserviceaccount.com
```

Restart both containers after creating or changing ADC because outbound replies can be sent by
either the API callback path or the chat worker.

## Incoming Request Verification

Google Chat sends interaction events to `POST /users/bots/google-chat/events` with:

```http
Authorization: Bearer <jwt>
```

The backend verifies the token using Google Chat's public certs for
`chat@system.gserviceaccount.com` and requires:

- JWT signature is valid.
- Issuer is `chat@system.gserviceaccount.com`.
- Audience is `GOOGLE_CHAT_AUTH_AUDIENCE`.

No service-account key is used for incoming request verification.

For local ngrok testing, set `GOOGLE_CHAT_AUTH_AUDIENCE` to the exact authentication audience
configured in Google Chat:

```text
GOOGLE_CHAT_AUTH_AUDIENCE=https://caitlyn-nonarmigerous-uninstructively.ngrok-free.dev/users/bots/google-chat/events
```

The value must match the token audience exactly. A localhost URL, frontend URL, HTTP URL, old
ngrok URL, or trailing-path mismatch will make inbound requests fail with `401`.

## Outgoing Messages

Outbound replies use app authentication with ADC or WIF:

```python
google.auth.default(scopes=["https://www.googleapis.com/auth/chat.bot"])
```

The backend then calls Google Chat REST API under:

```text
https://chat.googleapis.com/v1
```

The `https://www.googleapis.com/auth/chat.bot` scope is app/service-account auth. It is not
the same as user OAuth consent for reading user-visible Chat data.

## Future Read API Scopes

These scopes are configured for future user OAuth read APIs:

- `https://www.googleapis.com/auth/chat.spaces.readonly`
- `https://www.googleapis.com/auth/chat.memberships.readonly`
- `https://www.googleapis.com/auth/chat.messages.readonly`

Those read-only scopes require a user/admin consent token flow before the backend can read
broader user-visible Google Chat data.
