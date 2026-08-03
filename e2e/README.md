# End-to-End Tests

These tests run against a deployed backend over HTTP. They are intentionally outside
`tests/` so local unit and integration runs stay fast and isolated.

The authoritative design, safety rules, fixture model, evidence requirements, and
scenario catalogue are in
[`docs/Technical Documentation/testing/deployed-end-to-end-testing-framework.md`](../docs/Technical%20Documentation/testing/deployed-end-to-end-testing-framework.md).

## Authentication Smoke Test

Store the dedicated E2E user's credentials in the dev Key Vault as
`e2e-user-email` and `e2e-user-password`. After `az login`, run locally with:

```powershell
.\scripts\e2e\run-dev.ps1 auth
```

The runner reads both credentials from Key Vault without writing them to a local file.
The suite logs in at runtime and keeps the short-lived access token only in process
memory. CI sets `E2E_REQUIRE_CONFIG=1` so missing configuration fails instead of skips.

## CRM Authorization and Shared Jobs

After the exact backend revision has been deployed to dev, run:

```powershell
.\scripts\e2e\run-dev.ps1 crm-security
```

The runner loads four dedicated CRM actors, stable deal/meeting fixture IDs, and a
read-only `tenant_data` database credential from `copilot-master-kv-dev`. Selecting
this suite is the explicit live-mutation opt-in. The suite independently requires
the `dev` target and allowlisted `devapi.copilotgtm.com` host, so changing an API URL
cannot silently redirect mutations to production.

The current scenarios prove:

- deal-summary generation is shared only with an authorized collaborator and
  reaches a durable terminal result;
- same-organisation non-collaborators and foreign organisations cannot create or
  stream the protected job;
- meeting streams bind the complete deal, event, and supported-job-type identity;
- unsupported follow-up work and mismatched meeting creation create no job; and
- completed results remain available through the authenticated SSE recovery path.

Required Key Vault secret names are declared in `scripts/e2e/run-dev.ps1`. Missing
fixtures fail before the suite runs; they are never substituted with customer data.

## Gmail Mailbox Policy

After the revision is deployed to dev, run the lifecycle against a dedicated Gmail
account and integration:

```powershell
.\scripts\e2e\run-dev.ps1 gmail-sync
```

The seeder OAuth client is separate from the read-only application integration and is
consented for Gmail modification only on the dedicated mailbox. The scenario inserts
inbox, archive, sent, draft,
attachment, and excluded-trash fixtures; dispatches the real single-integration task;
and verifies the exact organisation/integration rows through a read-only database
credential. It then moves messages across the inclusion boundary, verifies the global
history cursor and soft deletion, reruns for idempotency, and removes only the seeded
provider messages.

This is smoke coverage for a subset of `GMAIL-E2E-001` and `GMAIL-E2E-002`; it does
not mark either scenario complete. The remaining mailbox classes and draft lifecycle,
plus the expired-cursor, pagination, and safety-cap checks in `GMAIL-E2E-003`, are not
implemented by this test.

The suite independently allowlists the dev API and integrations-broker host. To use a
fresh integration as explicit initial-sync evidence, set
`E2E_GMAIL_REQUIRE_INITIAL=true`; the test fails if that fixture already has a Gmail
cursor. Without that flag the same lifecycle remains repeatable as incremental-sync
evidence. Required Key Vault secret names are listed in the runner.

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
$env:E2E_CRM_DEAL_ID="dedicated-internal-deal-uuid"
$env:E2E_CRM_DEAL_NAME="Exact E2E Deal Name"
$env:E2E_USER_EMAIL="dedicated-e2e-user@example.com"
$env:E2E_USER_PASSWORD="..."
$env:E2E_WHATSAPP_DELIVERY_TIMEOUT_SECONDS="180" # optional
uv run pytest e2e/whatsapp/test_delivery_pipeline.py -m e2e -v
uv run pytest e2e/whatsapp/test_note_tools.py -m e2e -v
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

The note-tool scenario creates a uniquely titled internal deal note through WhatsApp,
updates that same note through WhatsApp, verifies that no duplicate row was created, and
soft-deletes the synthetic note through the authenticated API in a `finally` cleanup.

## Web Chat Delivery

Run against a dedicated non-production user and existing chat session:

```powershell
$env:E2E_API_BASE_URL="https://your-deployed-api.example.com"
$env:E2E_TARGET_ENVIRONMENT="dev"
$env:E2E_WEB_CHAT_ENABLE_LIVE_DELIVERY="true"
$env:E2E_USER_EMAIL="..."
$env:E2E_USER_PASSWORD="..."
$env:E2E_WEB_CHAT_SESSION_ID="..."
$env:E2E_WEB_CHAT_DATABASE_URL="postgresql://read_only_user:...@host/database"
$env:E2E_WEB_CHAT_ORG_SCHEMA="org_schema_name"
$env:E2E_CRM_DEAL_ID="dedicated-internal-deal-uuid"
$env:E2E_CRM_DEAL_NAME="Exact E2E Deal Name"
uv run pytest e2e/web_chat -m e2e -v
```

The web-chat suite verifies:

- invalid authentication, empty content, and cross-user/session access are rejected;
- the accepted response contains the job, user-message, and assistant-message IDs;
- the exact input reaches the durable job and its enqueue attempt has a Celery task ID;
- the completed result and persisted assistant message contain the same non-empty answer;
- three rapidly accepted messages create three distinct jobs and three durable outputs.

`e2e/web_chat/test_note_tools.py` uses the same create-update-no-duplicate contract as
WhatsApp and performs authenticated API cleanup even when an assertion fails.

The database credential is read-only. The suite blocks production targets and requires
an explicit live-delivery opt-in because successful cases invoke the real agent stack.
