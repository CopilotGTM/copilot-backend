# Deprecated Features

This folder contains features that have been deprecated but preserved for future use.

---

## HubSpot & Salesforce CRM Sync Pipeline

**Status**: Deprecated — sync removed, will be repurposed for one-time data migration

**Reason**: Building internal CRM. HubSpot/Salesforce ongoing sync is no longer needed. Code will be adapted for one-time migration scripts in the future.

### Deprecated Files

#### Worker Providers (API fetchers)
- `workers/integrations_worker/providers/crm/` — HubSpot and Salesforce fetcher modules

#### Worker Tasks (Celery three-tier sync)
- `workers/integrations_worker/tasks/crm/` — All CRM sync tasks (deals, contacts, companies, users, pipelines, activities, enrichment)

#### Worker Adapters (data transformation)
- `workers/integrations_worker/adapters/crm/` — Entity adapters
- `workers/integrations_worker/adapters/crm_adapter.py` — Main CRM adapter

#### Service Layer (sync + enrichment)
- `service/crm_sync/` — CRM entity sync services and enrichment orchestrator
- `service/crm_sync_service.py` — Main CRM sync service
- `service/crm_deal_sync_service.py` — CRM deal sync service

#### Tests
- `tests/service/integrations/crm/` — CRM sync service tests

### What Was Updated (not moved)
- `src/workers/integrations_worker/providers/registry.py` — CRM entries removed
- `src/workers/integrations_worker/celery_app.py` — CRM task import removed
- `src/workers/integrations_worker/config.py` — CRM task routes and beat schedule removed
- `src/service/integrations/initial_sync_service.py` — CRM sync trigger removed
- `src/infra/config/integrations_metadata.py` — HubSpot/Salesforce marked as DEPRECATED

### What Was NOT Deprecated
- **Domain models** (`src/domain/models/organisation_tables/sync/crm/`) — tables still contain data
- **API routes** for reading CRM data — still used by agents and frontend
- **Internal CRM services** (`src/service/crm/internal/`) — used for internal CRM
- **IntegrationType enum values** (HUBSPOT, SALESFORCE) — still referenced in DB records
- **Provider categories and Nango mappings** — kept for completeness

### When to Restore
When ready to build one-time migration scripts:
1. Adapt fetcher code from `deprecated/workers/integrations_worker/providers/crm/`
2. Create migration scripts in `scripts/maintenance/`
3. No need to restore sync tasks or beat schedule — migration is one-time

**Date Deprecated**: 2026-04-03

---

## Contacts & Companies CRUD (Step 4 - Pending)

**Status**: Deferred until frontend implementation begins

**Reason**: Step 4 (Contacts and Companies CRUD) is pending in the stepwise integration plan. These endpoints were built but the frontend has not yet implemented the UI for contacts/companies management.

### Deprecated Files

#### Contacts CRUD
- API Handler: `api/routers/crm/contacts_handler.py`
- Service: `service/crm/internal/crm_contact_service.py`
- Schema: `api/schemas/crm_contact_models.py`

#### Companies CRUD
- API Handler: `api/routers/crm/companies_handler.py`
- Service: `service/crm/internal/crm_company_service.py`
- Schema: `api/schemas/crm_company_models.py`

#### Shared Resources
- Documentation: `docs/crm-contacts-companies-api.md`
- Tests: `tests/test_internal_crm_write_services.py`

### When to Restore

When Step 4 implementation begins:
1. Move files back from `deprecated/` to `src/`
2. Re-register routers in `src/api/fastapi_app.py`
3. Re-add imports to `src/api/routers/crm/__init__.py`
4. Run tests to ensure functionality still works
5. Remove from `.gitignore`

### Note

Pipeline endpoints (`pipelines_handler.py`) were **NOT** deprecated as they provide READ-ONLY endpoints used in Step 1 (Pipeline Load) which is already complete and working in production.

**Date Deprecated**: 2026-01-22
**Planning Document**: `docs/Development Documentation/Planning Documents/internal-crm-frontend-stepwise-integration.md`
