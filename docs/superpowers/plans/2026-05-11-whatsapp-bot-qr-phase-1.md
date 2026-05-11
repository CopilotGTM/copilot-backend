# WhatsApp Bot QR Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-level WhatsApp bot discovery/connect UX using a QR/click-to-chat link to the configured CopilotGTM WhatsApp bot number.

**Architecture:** Backend extends the existing Integrations > Bots surface to include WhatsApp and a connect endpoint that returns a `wa.me` URL with prefilled `hi`. Frontend renders WhatsApp next to Slack and opens a QR modal from the connect URL. Existing WhatsApp webhook/auth-link logic remains the identity-linking mechanism after the user sends `hi`.

**Tech Stack:** FastAPI, Pydantic, SQLModel, WhatsApp Cloud API webhook service, Next.js, TypeScript.

---

### Task 1: Backend WhatsApp Bot Config And API Contract

**Files:**
- Modify: `src/api/schemas/integrations/bot_integration_schemas.py`
- Modify: `src/service/integrations/bot_integration_metadata.py`
- Modify: `src/service/integrations/bot_integration_service.py`
- Modify: `src/api/routers/integrations/bot_integration_routes.py`
- Modify: `src/infra/config/configs.py`
- Modify: `charts/application/values-dev.yaml`
- Modify: `charts/application/values-prod.yaml`
- Test: `tests/service/integrations/test_bot_integration_service.py`
- Test: `tests/api/routers/integrations/test_bot_integration_routes.py`
- Test: `tests/infra/config/test_integration_redirect_uris.py`

- [ ] Add failing tests that expect WhatsApp in `/bots/available`, a WhatsApp `wa.me` connect URL, and dev/prod `WHATSAPP_BOT_PHONE_NUMBER` values.
- [ ] Implement `WHATSAPP_BOT_PHONE_NUMBER` config and Helm values:
  - Dev: `+15556300848`
  - Prod: `+918168144213`
- [ ] Extend bot schemas to allow provider `whatsapp` and connection mode `qr_deep_link`.
- [ ] Extend service metadata with `CopilotGTM WhatsApp Bot`.
- [ ] Add `GET /users/integrations/bots/whatsapp/connect` returning `https://wa.me/<digits>?text=hi`.
- [ ] Re-run backend focused tests and Ruff.

### Task 2: Frontend WhatsApp Bot QR Connect

**Files:**
- Modify: `src/modules/integrations/types/integration.types.ts`
- Modify: `src/modules/integrations/types/api.types.ts`
- Modify: `src/modules/integrations/api/integrations.api.ts`
- Modify: `src/modules/integrations/services/integration.service.ts`
- Modify: `src/app/(root)/(dashboard)/integrations/page.tsx`

- [ ] Add WhatsApp bot/provider types and connect API.
- [ ] Add WhatsApp fallback bot card only when backend bot API is unavailable.
- [ ] Render WhatsApp beside Slack in the Bots section.
- [ ] On WhatsApp connect, show a QR modal with the backend `wa.me` URL and a direct open button.
- [ ] Re-run `yarn check-types`.

### Task 3: Documentation, Commit, Push, PR Update

**Files:**
- Modify: `docs/API Documentation/integrations-api.md`

- [ ] Document WhatsApp bot card and connect endpoint.
- [ ] Commit backend and frontend follow-up changes on `feat/named-bots-integrations`.
- [ ] Push both branches so PR #686 and PR #687 receive new iterations.
