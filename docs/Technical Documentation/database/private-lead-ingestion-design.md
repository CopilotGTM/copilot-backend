# Private Lead Ingestion API Design

**Date:** May 3, 2026
**Status:** Historical design; superseded for the primary mapped-form path
**Scope:** Server-to-server lead ingestion for Meta Ads, cal.com, n8n, Zapier, and customer-owned automations.

> Current direction: lead intake is form-first. The primary customer-facing
> private API is `POST /private/v1/forms/submissions` using a form-scoped
> `public_ingress_keys` row with `key_type = private_api`,
> `resource_type = lead_form`, and `resource_id = lead_forms.form_id`. The
> `/private/v1/crm/leads` endpoint remains as the advanced Direct Lead API for
> normalized lead payloads and does not use lead-form mappings.

## Decision

Build a separate private lead ingestion surface instead of reusing public lead form
URLs.

Public website forms remain keyed by `public_form_key` and are backed by
`lead_forms`, `lead_form_fields`, and `lead_form_submissions`. Private integrations
use an organisation-level key id plus secret, submit an already-normalized lead
payload, and create `CRMLead` records directly.

This keeps the two intake modes clear:

| Intake | Caller | Auth | Payload | Lead source |
| --- | --- | --- | --- | --- |
| Website form | Browser or website backend | `public_form_key` locator plus origin/rate policy | Form field map | `CRMLeadSourceType.FORM` |
| Private ingestion | Meta/cal.com/n8n/Zapier/customer automation | `X-CopilotGTM-Key-Id` plus bearer secret | Normalized lead object | `CRMLeadSourceType.API` or caller-supplied source |

## Goals

- Provide one stable server-to-server endpoint for external automation callers.
- Avoid provider-specific Meta or cal.com parsing in v1.
- Reuse `public.public_ingress_keys` for key metadata, policy, and lifecycle.
- Store only hashed secrets and return the raw secret only once at creation time.
- Preserve strict organisation isolation by resolving organisation context from the key.
- Return an external API response contract, not the app `GenericResponse` envelope.
- Make retries safe through idempotency.

## Non-Goals

- No direct Meta webhook verification or lead-fetching in this step.
- No provider-specific payload parser in the private endpoint.
- No frontend implementation beyond authenticated key management API contracts.
- No reuse of `LeadFormSubmission` for private API leads.

## Existing Repo Fit

The current public form implementation already provides the storage foundation:

- `PublicIngressKey` lives in the public schema and stores `organisation_id`,
  `key_type`, `resource_type`, `resource_id`, `public_key`, `secret_hash`,
  `allowed_origins`, `allowed_ips`, `rate_limit_policy`, `expires_at`,
  `last_used_at`, and `revoked_at`.
- `PublicIngressKeyType.PRIVATE_API` and
  `PublicIngressResourceType.LEAD_INGESTION` already exist.
- `CRMLeadSourceType.API` already exists.
- `CRMLead` already has normalized lead columns plus `attribution`, `consent`,
  `properties`, and `metrics`.

The private ingestion implementation should not call
`PublicLeadFormSubmissionService`. Public forms are definition-driven and versioned;
private ingestion is contract-driven and normalized.

## API Surface

### Authenticated Key Management

Base path: `/users/crm`

These endpoints require the existing user auth dependency and operate only within
the authenticated user's organisation.

#### Create Key

`POST /lead-ingestion-keys`

Request:

```json
{
  "name": "Meta Ads production",
  "allowed_ips": ["203.0.113.10"],
  "expires_at": null,
  "rate_limit_policy": {
    "per_minute": 300,
    "per_hour": 10000
  }
}
```

Response:

```json
{
  "key_id": "skid_2mYpWq7t5nQeVd4H",
  "secret": "cgtm_live_hK0n...",
  "status": "active",
  "created_at": "2026-05-03T10:00:00Z"
}
```

Rules:

- `public_key` stores the key id, prefixed with `skid_`.
- `secret_hash` stores an HMAC-SHA256 digest of the generated secret using a
  server-side pepper from secrets/config.
- The raw secret is returned only in this create response.
- `resource_id` is the organisation id for v1 org-level ingestion keys.
- `key_metadata` stores display metadata such as name and created-by user.

#### List Keys

`GET /lead-ingestion-keys`

Response contains key ids, status, metadata, expiry, last-used timestamp, and
policy fields. It never returns `secret` or `secret_hash`.

#### Update Status

`PATCH /lead-ingestion-keys/{key_id}/status`

Request:

```json
{
  "status": "paused"
}
```

Allowed status transitions:

- `active` -> `paused`
- `paused` -> `active`
- `active` or `paused` -> `revoked`

Revoked keys set `revoked_at` and cannot be reactivated.

### Private Lead Ingestion

Base path: `/private/v1`

`POST /crm/leads`

Headers:

```http
X-CopilotGTM-Key-Id: skid_2mYpWq7t5nQeVd4H
Authorization: Bearer cgtm_live_hK0n...
Idempotency-Key: meta-lead-98765
```

`Idempotency-Key` is required for callers that can retry. For v1, reject missing
idempotency keys unless the key's `rate_limit_policy` explicitly allows
non-idempotent ingestion.

Request:

```json
{
  "source_type": "api",
  "source_channel": "meta_ads",
  "source_name": "Q2 demo campaign",
  "source_ref": "campaign_123",
  "first_name": "Asha",
  "last_name": "Rao",
  "full_name": "Asha Rao",
  "email": "asha@example.com",
  "phone": "+14155550100",
  "job_title": "VP Sales",
  "company_name": "ExampleCo",
  "company_domain": "example.com",
  "company_website": "https://example.com",
  "message": "Interested in a demo",
  "utm_source": "meta",
  "utm_medium": "paid_social",
  "utm_campaign": "q2_demo",
  "attribution": {
    "ad_id": "ad_456",
    "form_id": "form_789"
  },
  "consent": {
    "marketing": true,
    "captured_at": "2026-05-03T10:00:00Z"
  },
  "properties": {
    "lead_temperature": "warm",
    "raw_source": "caller-normalized payload or provider replay reference"
  },
  "metrics": {
    "meta_quality_score": 0.92
  }
}
```

Success response:

```json
{
  "lead_id": "9c39f11e-0fa7-45c0-bd94-44cd7d39f191",
  "status": "accepted"
}
```

Error response:

```json
{
  "error": {
    "code": "invalid_secret",
    "message": "Invalid ingestion credentials",
    "field": null
  }
}
```

## Authentication And Key Validation

Validation order:

1. Read `X-CopilotGTM-Key-Id`; reject missing or malformed values.
2. Read `Authorization: Bearer ...`; reject missing bearer secret.
3. Load `PublicIngressKey` from the public schema by `public_key`.
4. Require:
   - `key_type = private_api`
   - `resource_type = lead_ingestion`
   - `status = active`
   - `resource_id = organisation_id`
   - `secret_hash IS NOT NULL`
   - `expires_at IS NULL OR expires_at > now()`
5. Validate the raw secret by recomputing HMAC-SHA256 and using
   `hmac.compare_digest`.
6. Validate `allowed_ips` against a trusted client IP.
7. Enforce Redis rate limits scoped by key id.
8. Update `last_used_at` only after authentication succeeds.

Do not log raw secrets, bearer headers, or full request bodies.

## Trusted Client IP

Private key `allowed_ips` must not trust arbitrary `X-Forwarded-For` headers.

Add a single helper for external ingress routes:

- Prefer the platform-provided trusted header only when the request is known to
  have passed through the configured proxy or gateway.
- Otherwise use `request.client.host`.
- Keep the helper shared by public form rate limits and private ingestion
  allowlists so the behavior is consistent.

If the deployment cannot guarantee trusted proxy headers, `allowed_ips` should be
documented as best-effort and enforced at the edge gateway instead.

## Rate Limits

Default private limits:

- `RATE_LIMIT_PRIVATE_LEAD_INGESTION_PER_MINUTE`, default `300`
- `RATE_LIMIT_PRIVATE_LEAD_INGESTION_PER_HOUR`, default `10000`

Use `PublicIngressKey.rate_limit_policy` to override per key:

```json
{
  "per_minute": 600,
  "per_hour": 20000
}
```

Redis key scope:

```text
rl:private_lead_ingestion:{key_id}:m:{minute_bucket}
rl:private_lead_ingestion:{key_id}:h:{hour_bucket}
```

On limit breach, return `429` with `Retry-After` and the external error payload.

## Idempotency

Private ingestion must be retry-safe.

V1 behavior:

- Require `Idempotency-Key` by default.
- Store the idempotency key in the dedicated `CRMLead.idempotency_key` column.
- Store the ingress key id in the dedicated `CRMLead.ingestion_key_id` column.
- Mirror ingestion diagnostics in `CRMLead.properties._copilotgtm_ingestion`.
- Before creating a lead, check whether a non-deleted lead already exists for:
  - current organisation
  - `source_type = api`
  - matching `ingestion_key_id`
  - matching `idempotency_key`
- If found, return the existing `lead_id` with status `accepted`.

The dedicated replay columns are the lookup contract; do not use JSONB traversal
for idempotency.

## Payload Validation

The private request must validate more than basic Pydantic shape:

- Max total JSON body size.
- Max object depth.
- Max list length.
- Max total node count.
- String length limits aligned with `CRMLead` column sizes.
- Email normalization and basic email format validation.
- Domain normalization for `company_domain`.
- Non-negative numeric validation for score, employee count, revenue, and budget.
- Require at least one of `email`, `phone`, `full_name`, or `company_name`.

Unknown fields should be rejected. Extra caller diagnostics belong under
`properties`, not at the top level.

## Service Boundaries

### `LeadIngestionKeyService`

Location:

`src/service/crm/access/lead_ingestion_key_service.py`

Responsibilities:

- Create private ingestion key ids and secrets.
- Hash secrets with HMAC-SHA256.
- List key metadata without exposing secrets.
- Pause, activate, and revoke keys.
- Enforce organisation ownership for key-management operations.

### `PrivateLeadIngestionAuthService`

Location:

`src/service/crm/access/private_lead_ingestion_auth_service.py`

Responsibilities:

- Authenticate key id plus bearer secret.
- Validate key lifecycle, expiry, type, resource, and IP policy.
- Enforce key-scoped rate limits.
- Stamp `last_used_at`.
- Return resolved `organisation_id`.

### `PrivateLeadIngestionService`

Location:

`src/service/crm/access/private_lead_ingestion_service.py`

Responsibilities:

- Validate normalized lead payload semantics.
- Apply idempotency lookup.
- Create `CRMLead` in the resolved organisation schema.
- Set `source_type = CRMLeadSourceType.API` by default.
- Store ingestion metadata in dedicated replay columns and mirrored
  `properties._copilotgtm_ingestion`.

## Routing

Add routers:

- `src/api/routers/crm/leads/lead_ingestion_keys_handler.py`
- `src/api/routers/crm/leads/private_lead_ingestion_handler.py`

Register:

- `/users/crm/lead-ingestion-keys` for authenticated key management.
- `/private/v1/crm/leads` for server-to-server ingestion.

The private router should not use `GenericResponse`.

## Data Mapping

Create a lead with:

- `organisation_id` from the authenticated key.
- `source_type = api` unless the request supplies a supported source enum.
- `source_channel`, `source_name`, and `source_ref` from request.
- Standard lead fields copied directly after normalization.
- `attribution`, `consent`, `properties`, and `metrics` copied from request.
- `properties` merged with reserved ingestion diagnostics:

```json
{
  "_copilotgtm_ingestion": {
    "key_id": "skid_2mYpWq7t5nQeVd4H",
    "idempotency_key": "meta-lead-98765",
    "received_at": "2026-05-03T10:00:00Z"
  }
}
```

Do not set `form_id`, `form_version_id`, or `form_submission_id` for private API
leads.

## Security Requirements

- Never store raw secrets.
- Never return secrets after creation.
- Never log `Authorization`, raw secret, or full body.
- Use constant-time secret comparison.
- Enforce key type, resource type, status, expiry, and organisation ownership.
- Keep private API error messages generic for auth failures.
- Apply rate limits after key lookup but before database writes.
- Validate trusted client IP before rate limit and lead creation.
- Reject unknown top-level fields.
- Keep public forms unchanged except for shared trusted-IP helper improvements.

## Observability

Log structured events without secrets:

- `lead_ingestion_key_created`
- `lead_ingestion_key_status_changed`
- `private_lead_ingestion_auth_failed`
- `private_lead_ingestion_rate_limited`
- `private_lead_ingestion_accepted`
- `private_lead_ingestion_idempotent_replay`

Suggested metrics:

- accepted count by key id hash and source channel
- auth failure count by reason
- rate-limit count
- idempotent replay count
- validation failure count by error code

## Test Plan

Key management:

- Key creation returns raw secret once and stores only a hash.
- List endpoint never returns raw secret or `secret_hash`.
- Status endpoint pauses, activates, and revokes keys.
- Revoked key cannot be reactivated.
- Cross-organisation key management is rejected.

Authentication:

- Valid key id plus secret resolves the correct organisation.
- Invalid key id returns external auth error.
- Bad secret returns external auth error.
- Paused, revoked, and expired keys are rejected.
- Disallowed IP is rejected.
- `last_used_at` updates only after successful authentication.

Ingestion:

- Valid payload creates a `CRMLead` in the correct org schema.
- Missing identity fields are rejected.
- Unknown top-level fields are rejected.
- Oversized, too-deep, or too-wide payloads are rejected.
- Idempotent retry returns the existing lead id.
- Missing `Idempotency-Key` is rejected by default.

Rate limiting:

- Key-scoped minute and hour limits are enforced.
- `429` responses include `Retry-After`.
- Rate-limit errors use the private external error contract.

Regression:

- Existing public form tests remain unchanged.
- Public form submissions still create `CRMLeadSourceType.FORM` leads.
- Public form response still hides internal `lead_id`.

Suggested commands before PR:

```bash
uv run pytest tests/service/crm/test_lead_forms_service.py tests/api/test_public_lead_form_rate_limit.py
uv run pytest tests/service/crm/test_private_lead_ingestion_service.py tests/api/test_private_lead_ingestion_handler.py
uv run ruff check src/ tests/
uv run pyright
```

## Implementation Order

1. Add schemas for key management and private lead ingestion payloads.
2. Add key-generation and HMAC hashing helpers.
3. Add `LeadIngestionKeyService`.
4. Add trusted client IP helper and migrate public form rate limits to it.
5. Add private auth service with key validation, IP policy, rate limits, and
   `last_used_at`.
6. Add private ingestion service with payload validation and idempotency lookup.
7. Add authenticated key-management router.
8. Add `/private/v1/crm/leads` router and app registration.
9. Add focused unit/API tests.
10. Update `docs/API Documentation/` for the new key-management and private
    ingestion contracts.
