# Named Bots Integrations Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a user-facing Bots section under Integrations so users can connect named CopilotGTM bots, starting with Slack and preparing WhatsApp without mixing bot installs into data-sync OAuth integrations.

**Architecture:** Keep runtime bot traffic on the existing `/users/bots/*` routes and bot services. Add an authenticated `/users/integrations/bots/*` management surface that returns provider cards, connect links/instructions, installation status, and per-user identity-link status by reading `bot_platform_installations` and `external_identity_mappings`.

**Tech Stack:** FastAPI, SQLModel/SQLAlchemy async sessions, PostgreSQL public schema, existing bot services, Slack OAuth v2, Meta WhatsApp Cloud API.

---

## Research Summary

### Repo facts

- Existing integration list: `GET /users/integrations/available` is built from `IntegrationType` metadata and `knowledge_integrations`; it is designed for OAuth/data-sync integrations.
- Existing bot runtime: `/users/bots/*` is already mounted and includes Slack, WhatsApp, Teams, Google Chat, and generic bot auth callback routes.
- Existing installation storage: `public.bot_platform_installations` tracks workspace-level bot installs for Teams, Slack, and Google Chat. It does not currently include WhatsApp.
- Existing identity storage: `public.external_identity_mappings` already supports `teams`, `slack`, `whatsapp`, and `google_chat`.
- Existing Slack bot install flow: `GET /users/bots/slack/oauth/callback` exchanges Slack OAuth v2 codes and stores the workspace bot token in `bot_platform_installations`.
- Existing WhatsApp runtime: `/users/bots/whatsapp/webhook` verifies Meta webhooks and `WhatsAppBotService` can route inbound messages, media, audio, identity login, and outbound replies using global WhatsApp credentials.

### Provider facts

- Slack apps should use OAuth v2 installation. The install URL is `https://slack.com/oauth/v2/authorize` with bot scopes in `scope`; Slack returns through a configured redirect URL and `oauth.v2.access` returns the bot token and team data.
- Slack Events API can deliver events to a public HTTPS Request URL; subscribed events are tied to granted OAuth scopes.
- Slack app manifests support `oauth_config.redirect_urls` and bot scopes under `oauth_config.scopes.bot`.
- WhatsApp has two realistic onboarding paths:
  - Manual Cloud API setup: user/admin provides WABA/phone-number configuration and tokens, while our backend already has webhook verification and send-message support.
  - Embedded Signup: best UX for self-serve WhatsApp onboarding, but it requires Meta app/business setup, relevant permissions such as `business_management` and `whatsapp_business_management`, app review/advanced access, and callback/session handling.
- For this repo, manual WhatsApp connection is the lower-risk first step because runtime support already assumes shared configured credentials, not per-customer WABA credential storage.

## Product Decision

Expose a top-level Bots category in the integrations UI/API, but model bots separately from normal integrations.

Do not create `IntegrationType.WHATSAPP` for the first pass. WhatsApp is not a data-sync OAuth integration in this repo; it is a bot messaging channel backed by the WhatsApp Cloud API runtime. Adding it to `IntegrationType` would mislead the frontend into using the Nango/OAuth path.

Recommended first provider order:

1. Slack bot connect: fully actionable now.
2. Teams bot link: include read-only metadata because Teams bot runtime already exists, but do not modify the separate Microsoft integration app.
3. WhatsApp manual setup: expose status and required values/instructions first.
4. WhatsApp Embedded Signup: plan as a later provider-owned onboarding upgrade after Meta app prerequisites are confirmed.

## API Shape

### `GET /users/integrations/bots/available`

Returns all bot provider cards for the authenticated user's organization.

```json
{
  "bots": [
    {
      "provider": "slack",
      "name": "Slack Bot",
      "display_name": "CopilotGTM Slack Bot",
      "description": "Chat with CopilotGTM from Slack DMs, mentions, and supported workflows.",
      "category": "bots",
      "availability_status": "available",
      "connection_mode": "oauth_install",
      "connect_url": "https://slack.com/oauth/v2/authorize?...",
      "installed": true,
      "installation": {
        "installation_id": "uuid",
        "workspace_id": "T123",
        "workspace_name": "Acme",
        "installed_at": "2026-05-11T00:00:00Z",
        "last_used_at": "2026-05-11T01:00:00Z"
      },
      "identity_linked": true,
      "identity_mapping_id": "uuid",
      "action_required": "none"
    },
    {
      "provider": "whatsapp",
      "name": "WhatsApp Bot",
      "display_name": "CopilotGTM WhatsApp Bot",
      "description": "Chat with CopilotGTM from WhatsApp after the business number is configured.",
      "category": "bots",
      "availability_status": "beta",
      "connection_mode": "manual_setup",
      "connect_url": null,
      "installed": false,
      "installation": null,
      "identity_linked": false,
      "identity_mapping_id": null,
      "action_required": "setup_required"
    }
  ],
  "total_available": 2,
  "total_installed": 1,
  "total_linked": 1
}
```

### `GET /users/integrations/bots/slack/connect`

Returns the Slack install URL. This keeps URL construction backend-owned so scopes, redirect URL, and state are not duplicated in the frontend.

```json
{
  "provider": "slack",
  "connect_url": "https://slack.com/oauth/v2/authorize?client_id=...&scope=app_mentions:read,chat:write,im:history,im:read,im:write,users:read,users:read.email,files:read&redirect_uri=...",
  "expires_at": null
}
```

### `GET /users/integrations/bots/whatsapp/setup`

Returns setup requirements and current config status. Do not return secrets.

```json
{
  "provider": "whatsapp",
  "connection_mode": "manual_setup",
  "webhook_url": "https://api.copilotgtm.com/users/bots/whatsapp/webhook",
  "required_values": [
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_WEBHOOK_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_GRAPH_API_VERSION"
  ],
  "configured": false,
  "missing": ["access_token", "webhook_verify_token", "app_secret"],
  "notes": [
    "Use Meta Cloud API webhook subscription for messages.",
    "Configure the verify token in Meta and backend secrets with the same value.",
    "Use the app secret to verify X-Hub-Signature-256 on POST webhooks."
  ]
}
```

## File Map

- Create `src/api/schemas/integrations/bot_integration_schemas.py`
  - Pydantic response models for bot provider cards, install status, Slack connect URL, and WhatsApp setup requirements.
- Create `src/service/integrations/bot_integration_metadata.py`
  - Static metadata for `slack`, `teams`, `whatsapp`, and later `google_chat`.
  - Keep product names user-facing: "CopilotGTM Slack Bot", "CopilotGTM WhatsApp Bot".
- Create `src/service/integrations/bot_integration_service.py`
  - Reads public-schema bot installation rows and identity mappings.
  - Builds Slack install URL from `slack_config`.
  - Builds WhatsApp setup status from `whatsapp_config`.
- Create `src/api/routers/integrations/bot_integration_routes.py`
  - Authenticated management endpoints under `/users/integrations/bots`.
- Modify `src/api/routers/integrations/__init__.py`
  - Include the new bot integration router.
- Modify `src/domain/models/master_tables/bot_platform_installation_models.py`
  - Add `WHATSAPP = "whatsapp"` only if we decide to represent configured WhatsApp business numbers as bot installations.
- Modify `docs/API Documentation/integrations-api.md`
  - Document the Bots API and make the distinction from Nango/data-sync integrations explicit.
- Tests:
  - Create `tests/api/routers/integrations/test_bot_integration_routes.py`.
  - Create `tests/service/integrations/test_bot_integration_service.py`.

## Task 1: Add Bot Integration Schemas

**Files:**
- Create: `src/api/schemas/integrations/bot_integration_schemas.py`
- Modify: `src/api/schemas/integrations/__init__.py`

- [ ] Step 1: Define provider enums and response models.
- [ ] Step 2: Export the schemas from the integrations schema package.
- [ ] Step 3: Add model tests only if existing schema tests exist; otherwise cover through router tests.

## Task 2: Add Bot Metadata Registry

**Files:**
- Create: `src/service/integrations/bot_integration_metadata.py`

- [ ] Step 1: Add metadata for Slack as `available` and WhatsApp as `beta`.
- [ ] Step 2: Include Teams as `available` but mark `connection_mode = external_admin_setup` unless a direct app install link is already available.
- [ ] Step 3: Keep provider metadata independent from `IntegrationType`.

## Task 3: Add Bot Integration Service

**Files:**
- Create: `src/service/integrations/bot_integration_service.py`

- [ ] Step 1: Write failing tests for organization-scoped installation lookup.
- [ ] Step 2: Query `BotPlatformInstallation` by `organisation_id`, `provider`, and `is_active`.
- [ ] Step 3: Query `ExternalIdentityMapping` by `organisation_id`, `user_id`, provider, and `is_active`.
- [ ] Step 4: Build provider cards with `installed`, `identity_linked`, and `action_required`.
- [ ] Step 5: Build Slack connect URL using Slack OAuth v2 and bot scopes.
- [ ] Step 6: Build WhatsApp setup status without exposing secrets.

## Task 4: Add Authenticated Integration Routes

**Files:**
- Create: `src/api/routers/integrations/bot_integration_routes.py`
- Modify: `src/api/routers/integrations/__init__.py`

- [ ] Step 1: Add `GET /bots/available`.
- [ ] Step 2: Add `GET /bots/slack/connect`.
- [ ] Step 3: Add `GET /bots/whatsapp/setup`.
- [ ] Step 4: Ensure every route uses `get_current_user` and a master DB session where public schema data is needed.
- [ ] Step 5: Return standard `GenericResponse[...]` responses using existing response helpers.

## Task 5: Slack First-Class Connect

**Files:**
- Modify: `src/api/routers/bots/slack_webhook_handler.py`
- Modify: `src/service/bots/bot_installation_service.py`
- Tests: `tests/api/routers/bots/test_slack_oauth_callback.py` if missing.

- [ ] Step 1: Add OAuth `state` validation for Slack install callback if the new connect endpoint emits state.
- [ ] Step 2: Store any selected organization context only after validating the logged-in user owns that organization.
- [ ] Step 3: Keep the current identity-link-on-first-message flow as the fallback when install happens from Slack directly.
- [ ] Step 4: Verify Slack uninstall/deactivate behavior if app uninstall events are already subscribed; add handling if missing.

## Task 6: WhatsApp Manual Setup First

**Files:**
- Modify: `src/domain/models/master_tables/bot_platform_installation_models.py` only if representing WhatsApp phone numbers as installation rows.
- Modify: `src/service/integrations/bot_integration_service.py`
- Tests: `tests/service/integrations/test_bot_integration_service.py`

- [ ] Step 1: Decide whether the first pass stores WhatsApp status as config-only or as `BotPlatformInstallation(provider=whatsapp, external_workspace_id=phone_number_id)`.
- [ ] Step 2: If storing installations, add `WHATSAPP` to `BotPlatformProvider` and create a master migration.
- [ ] Step 3: Keep token values in Key Vault/env, not API responses.
- [ ] Step 4: Return the webhook URL and missing setup values for the frontend/admin.

## Task 7: WhatsApp Embedded Signup Follow-Up

**Files:**
- Future create: `src/api/routers/integrations/whatsapp_embedded_signup_routes.py`
- Future create: `src/service/integrations/whatsapp_embedded_signup_service.py`

- [ ] Step 1: Confirm Meta app type, business verification, app review, and permissions.
- [ ] Step 2: Add frontend popup/session handling only after backend can exchange and persist customer WABA/phone-number details.
- [ ] Step 3: Store per-customer WABA/phone-number metadata separately from global app credentials.
- [ ] Step 4: Subscribe the configured WABA/phone number to existing `/users/bots/whatsapp/webhook`.

## Task 8: Documentation and Verification

**Files:**
- Modify: `docs/API Documentation/integrations-api.md`

- [ ] Step 1: Add a "Bots Section" chapter with endpoint contracts and frontend behavior.
- [ ] Step 2: Explain that bot connections are separate from Nango OAuth/data-sync integrations.
- [ ] Step 3: Run targeted tests:

```bash
uv run pytest tests/service/integrations/test_bot_integration_service.py -v
uv run pytest tests/api/routers/integrations/test_bot_integration_routes.py -v
```

- [ ] Step 4: Before PR, run:

```bash
uv run ruff check src/ tests/
uv run mypy src/
```

## Open Decisions

1. Should the frontend show only Slack and WhatsApp first, or include Teams as an already-supported bot card?
2. Should Slack bot installation be org-admin-only, or can any member install and then link their identity?
3. For WhatsApp first pass, will CopilotGTM own one shared WhatsApp business number, or does each customer need to connect their own WABA/phone number?

## Source Links

- Slack OAuth v2 install: https://docs.slack.dev/authentication/installing-with-oauth
- Slack Events API: https://docs.slack.dev/apis/events-api/
- Slack app manifest OAuth fields: https://docs.slack.dev/reference/app-manifest
- Meta WhatsApp Embedded Signup Postman collection: https://www.postman.com/meta/whatsapp-business-platform/collection/du6gzjv/embedded-signup
- Meta WhatsApp Embedded Signup implementation summary: https://support.chatarchitect.com/books/meta-whatsapp/page/implementation-developer-documentation
