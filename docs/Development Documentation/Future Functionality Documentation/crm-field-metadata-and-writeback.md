# Provider Field Metadata & Write-Back: Analysis & Decision

Created: December 16, 2025
Purpose: Evaluate whether storing provider field metadata (custom field definitions) should be implemented now or deferred, and outline the design when we do it.

---

## Executive Summary

Storing provider field metadata (object field definitions, picklists, required flags, etc.) unlocks custom-field mapping UIs and safe write-back. It is strategic, but not required for read-only multi-CRM ingestion. Given current focus on stabilizing inbound sync and analytics, we recommend: Defer implementation until a concrete write-back roadmap or a customizable field-mapping UI is prioritized.

---

## What Is "Field Metadata"?

For each provider object (deal, contact, company, activity, etc.), capture the field definitions exposed by the CRM:

- API name, label, type (text, number, date, boolean, picklist, reference)
- Requirement/readonly, max length, precision/scale (numbers)
- Picklist options (values, labels, ordering, active flags)
- Visibility/permissions (where available)
- Provider-specific attributes (record type constraints, calculated, formula)

This metadata allows our product to:

- Render dynamic mapping UIs and forms
- Validate data before write-back (types, required, picklists)
- Normalize provider data more safely (especially enumerations)
- Cache schema for offline/low-rate-limit operations

---

## Primary Use Cases

1) Custom Field Mapping UI

- Let admins map provider fields to our normalized schema or product features.
- Show labels, types, and picklist options for easy, safe configuration.

1. Safe Write-Back (Create/Update in CRM)

- Validate payloads against the provider schema before calling APIs.
- Enforce picklist values and required fields.
- Avoid runtime errors and partial writes.

1. Picklist Rendering

- Render proper dropdowns with provider labels and ordering.
- Support value-label conversions for analytics and UI.

1. Field-Level Sync Configuration

- Allow opting in/out specific fields.
- Support per-field transforms or privacy controls.

1. Analytics & Data Quality

- Detect fields that are required but frequently empty.
- Identify custom fields for potential productization.

---

## Proposed Schema (Minimal Viable)

Table: crm_field_definitions

- field_definition_id (UUID, PK)
- organisation_id (UUID, indexed)
- integration_id (UUID, indexed)
- object_type (string: deal, contact, company, activity, user, lead, ticket, custom)
- field_key (string, provider API name; unique per (integration_id, object_type, field_key))
- field_label (string, provider display label)
- field_type (string: text, number, date, datetime, boolean, picklist, reference, currency, percent, longtext, textarea, email, phone, url, json, other)
- is_required (bool)
- is_readonly (bool)
- is_custom (bool)
- picklist_values (JSONB: [{"value","label","active","order"}])
- number_precision (int | null)
- number_scale (int | null)
- reference_to (JSONB or string: target object type(s), when available)
- properties (JSONB, default {}) – provider-specific extras (e.g., record type restrictions)
- source_created_at (timestamptz | null)
- source_modified_at (timestamptz | null)
- synced_at (timestamptz, not null)

Indexes

- (organisation_id, integration_id, object_type)
- (integration_id, object_type, field_key) unique

Notes

- Keep this table provider-owned (integration-scoped). It is not core-layer data.
- No FK to our normalized columns; this is an auxiliary catalog.

---

## Provider Coverage

- Salesforce: Describe Global + Describe SObject; supports record-type-aware picklists, requiredness, reference fields. Rate limits apply.
- HubSpot: CRM Properties API per object (deals, contacts, companies, tickets, custom objects); provides labels, types, options, and groups.
- Pipedrive: Fields API per object; supports custom fields with types and options.
- Zoho: Fields API per module; picklists, mandatory flags, and other constraints.

---

## Sync Strategy

- Scope: per-organisation, per-integration, per-object-type refresh.
- Frequency: daily or on-demand when admin opens mapping UI (lazy refresh), plus manual refresh button.
- Change detection: Compare modified timestamps when available; else re-fetch and diff.
- Storage policy: Replace-on-refresh for a given (integration, object_type); keep historical rows only if we need audits (initially unnecessary).
- Safety: Respect provider scopes/permissions; gracefully degrade if read of metadata is not permitted.

---

## How It’s Used In Product

- Mapping UI pulls `crm_field_definitions` for selected object_type.
- Write-back builders validate outgoing payloads, coercing to correct types and checking required/picklist constraints.
- Analytics can display human labels for picklists; internal pipelines can normalize provider enums with confidence.

---

## Triggers To Implement (When to Move From Future → Now)

- We commit to write-back (creating or updating CRM records from our product).
- We ship a field mapping UI (admin-configured mappings or transformations).
- Enterprise customers require rich picklist support/validation for workflows.
- Multiple CRMs per org with divergent custom fields that we must present coherently.

---

## Risks & Mitigations

- Volume/Rate Limits: Metadata endpoints can be heavy. Mitigate with caching, per-object refresh, lazy + manual refresh.
- Churn/Drift: Admins change fields/picklists often. Use `synced_at` and “refresh” UX, plus guardrails in write-back code.
- Permissions: Some endpoints need elevated scopes. Detect and degrade gracefully.
- Complexity: Avoid over-modeling; keep a single table with flexible JSON for provider specifics.

---

## Implementation Outline (When Greenlit)

1. Data Model + Migration

- Add `crm_field_definitions` as described above with indices and uniqueness constraints.

1. Sync Services

- Implement per-provider fetchers (Salesforce Describe, HubSpot Properties, etc.).
- Normalize field types into our canonical `field_type` and fill `properties` for specifics.

1. Caching & Refresh UX

- Background job to refresh nightly; manual refresh endpoint for admins.

1. UI Integration

- Mapping UI consumes `crm_field_definitions` to render fields and picklists.
- Label/value conversions for picklists are handled via metadata.

1. Write-Back Guards

- Builders validate payloads before API calls, using metadata.
- Graceful errors with actionable messages when values are invalid.

1. Observability

- Log metadata fetch durations and sizes; alert on large diffs.

---

## Decision

Defer for now. Revisit when write-back or field mapping UI becomes a near-term product goal, or when enterprise customers explicitly request it. The design above keeps scope tight and aligned with immediate value once triggered.
