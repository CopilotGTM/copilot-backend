# End-to-End Tests

These tests run against a deployed backend over HTTP. They are intentionally outside
`tests/` so local unit and integration runs stay fast and isolated.

## WhatsApp Delivery

Run:

```powershell
$env:E2E_API_BASE_URL="https://your-deployed-api.example.com"
$env:E2E_TARGET_ENVIRONMENT="dev"
$env:E2E_WHATSAPP_APP_SECRET="..."
$env:E2E_WHATSAPP_VERIFY_TOKEN="..."
$env:E2E_WHATSAPP_PHONE_NUMBER_ID="..."
$env:E2E_WHATSAPP_RECIPIENT_WA_ID="..."
uv run pytest e2e/whatsapp -m e2e -v
```

The WhatsApp suite checks the deployed webhook contract:

- Meta verification challenge returns the challenge as `text/plain`.
- Invalid signatures are rejected.
- Invalid JSON with a valid signature is rejected.
- Meta-shaped text webhooks are accepted with HTTP 200.
- Retried duplicate webhook payloads return HTTP 200.

The HTTP contract cases are safe to run without delivery access. Tests that create
jobs or send messages require an explicit non-production opt-in:

```powershell
$env:E2E_WHATSAPP_ENABLE_LIVE_DELIVERY="true"
$env:E2E_WHATSAPP_DATABASE_URL="postgresql://read_only_user:...@host/database"
$env:E2E_WHATSAPP_ORG_SCHEMA="org_schema_name"
$env:E2E_WHATSAPP_DELIVERY_TIMEOUT_SECONDS="180" # optional
uv run pytest e2e/whatsapp/test_delivery_pipeline.py -m e2e -v
```

Use a dedicated WhatsApp test number, recipient mapping, organisation, and read-only
database credential. The pipeline suite verifies:

- the exact inbound text and WhatsApp routing context persisted in the dispatch envelope;
- one durable job for a retried Meta message ID;
- generated outbound chunks are non-empty, ordered, and within the 3,900-character limit;
- every accepted chunk has Meta's provider message ID, acceptance timestamp, and 2xx status;
- successful jobs finish with `processing_stage=completed` and `send_status=sent`;
- a real Meta rejection finishes as a visible send-stage failure with status and error evidence.

`send_status=sent` means the Meta API accepted every outbound chunk. The backend does
not currently persist Meta `delivered` or `read` callbacks, so this suite does not claim
that a handset displayed the message.

## Web Chat Delivery

Run against a dedicated non-production user and existing chat session:

```powershell
$env:E2E_API_BASE_URL="https://your-deployed-api.example.com"
$env:E2E_TARGET_ENVIRONMENT="dev"
$env:E2E_WEB_CHAT_ENABLE_LIVE_DELIVERY="true"
$env:E2E_WEB_CHAT_AUTH_TOKEN="..."
$env:E2E_WEB_CHAT_SESSION_ID="..."
$env:E2E_WEB_CHAT_DATABASE_URL="postgresql://read_only_user:...@host/database"
$env:E2E_WEB_CHAT_ORG_SCHEMA="org_schema_name"
uv run pytest e2e/web_chat -m e2e -v
```

The web-chat suite verifies:

- invalid authentication, empty content, and cross-user/session access are rejected;
- the accepted response contains the job, user-message, and assistant-message IDs;
- the exact input reaches the durable job and its enqueue attempt has a Celery task ID;
- the completed result and persisted assistant message contain the same non-empty answer;
- three rapidly accepted messages create three distinct jobs and three durable outputs.

The database credential is read-only. The suite blocks production targets and requires
an explicit live-delivery opt-in because successful cases invoke the real agent stack.
