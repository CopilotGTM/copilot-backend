# CRM Deals Import API

> Version: 2.0  
> Base URL: `/users/crm/deals`  
> Last Updated: 2026-04-18

---

## Overview

The previous CSV import flow has been removed. The route names remain reserved so
the frontend can keep calling the same endpoints, but the backend now hard-stops
all import/template/event-stream requests with `409 Conflict` while the feature
is rebuilt.

---

## Disabled Endpoints

These endpoints are still registered:

- `GET /users/crm/deals/import/template`
- `POST /users/crm/deals/import/upload-url`
- `POST /users/crm/deals/import/process`
- `GET /users/crm/deals/import/jobs/{job_id}/events`

Each endpoint returns the same response:

### Response (409)

```json
{
  "status": "ERROR",
  "message": "Deal CSV import is coming soon. The previous import flow has been removed.",
  "data": null
}
```

---

## Frontend Guidance

- Do not expect template download links, upload URLs, processing jobs, or SSE streams.
- Treat any `409` from the endpoints above as a temporary product-state response, not a retryable import job failure.
- `GET /users/crm/deals` no longer exposes active import reconnect metadata while import is disabled.
