# Deprecated CRM Features

This folder contains CRM features that have been deprecated as they are not currently implemented in the frontend.

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
