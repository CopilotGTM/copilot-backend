# Private Lead Ingestion Production Structure

## Goal

Make `private_lead_ingestion_handler.py` a thin HTTP adapter. Response shaping, validation-error shaping, and structured failure-log payloads should live in an API presenter; direct private lead persistence should live behind an explicit lead write-service boundary.

## File Plan

### Create `src/api/presenters/crm/private_lead_ingestion_presenter.py`

Owns API presentation only:

- Build `PrivateLeadIngestionErrorResponse` JSON responses.
- Convert Pydantic `ValidationError` into `(message, field)`.
- Build structured warning-log payloads for private form failures.
- Build direct-ingestion and mapped-form success response models.

This keeps handler code from manually assembling response dictionaries and log `extra` payloads.

### Create `src/service/crm/leads/write_service.py`

Owns direct private lead writes:

- Move `PrivateLeadIngestionService` and `PrivateLeadIngestionServiceError` here.
- Keep idempotency, payload-budget validation, normalization, persistence, and unique-race replay logic together.
- Do not create `read_service.py` now. Private ingestion only writes leads; adding a read service without a read use case would be fake symmetry with deals.

### Create `src/service/crm/leads/__init__.py`

Expose the lead write-service boundary from a real lead service package.

### Modify `src/service/crm/access/private_lead_ingestion_service.py`

Keep this as a compatibility shim that imports from `src.service.crm.leads.write_service`.

This avoids a risky broad import rewrite and keeps existing callers/tests working while the production path exists in the correct package.

### Modify `src/api/routers/crm/leads/private_lead_ingestion_handler.py`

Keep only HTTP orchestration:

- Header checks.
- Auth service call.
- Write-scope check.
- Session acquisition.
- Service calls.
- Logging calls using presenter-built metadata.

Remove local response/presenter helpers from the router.

### Tests

Add focused tests for the new presenter helpers before implementation:

- Error response body/status/headers.
- Validation error field extraction.
- Structured private-form failure log payload.

Run existing private ingestion handler/service tests to ensure behavior stays compatible.

