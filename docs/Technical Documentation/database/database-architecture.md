# Database Architecture

This is the one document you can hand to any new engineer to design 80–90% of production‑grade tables for any microservice. It focuses on core principles, reusable patterns, and minimal templates—without deep domain specifics.

## Core Principles (read first)

- Multi‑tenant by default: every tenant‑scoped table carries `organisation_id` (UUID).
- Global vs tenant scope: master tables live in `public` schema; organisation tables live in the default schema and always include `organisation_id`.
- UUID primary keys: all entities use UUID PKs; no integer sequences.
- Time is first‑class: include `created_at` and `updated_at` (via mixins). Prefer server defaults and automatic `onupdate`.
- Soft delete over hard delete: use `is_deleted` and/or `is_archived` where removal is reversible; keep provenance.
- JSONB for flexible metadata: use typed columns for stable fields and JSONB for evolving/optional payloads.
- Index with intent: add indexes for FK columns and high‑cardinality filters; prefer composite indexes for common scopes.
- Clear ownership: add `user_id` when helpful to capture ownership or the actor who created the record.
- Consistent naming: predictable table, FK, index, and constraint names (see checklist below).

## Tenancy Model

- Tenancy key: `organisation_id` (UUID). All organisation tables include this to scope data per tenant.
- Identity keys: `user_id` (UUID for app user), `supabase_user_id` (string from auth provider), and domain‑specific IDs (e.g., `session_id`, `message_id`, `document_id`).
- Separation:
  - Master tables live in the `public` schema and store global/administrative entities: organisations, users, super admins, and organisation‑level integration connections.
  - Organisation tables store tenant data: chats, knowledge base, synced calendar/CRM items, jobs, and per‑tenant integrations.
- Soft deletes: Many org tables include `is_deleted` and/or `is_archived` to support non‑destructive removal and archival.

## Master Tables (Global)

Located under `src/domain/models/master_tables`. Key models:

- Organisations (`Organisation`) — organisation registry and lifecycle
  - Path: `src/domain/models/master_tables/organisation_models.py`
  - Key fields: `organisation_id` (PK), `organisation_name`, `status`, `is_active`, audit relationships.
  - Related: `OrganisationActionHistory` for state changes and admin actions.

- Users (`User`) — users that belong to organisations
  - Path: `src/domain/models/master_tables/user_models.py`
  - Key fields: `user_id` (PK), `organisation_id` (FK to organisation), `supabase_user_id`, `email`, `user_role`, `admin_status`.
  - Related: `UserActionHistory` for lifecycle and security events.

- Super Admins (`SuperAdmin`) — platform administrators
  - Path: `src/domain/models/master_tables/super_admin_models.py`
  - Key fields: `admin_id` (PK), `supabase_user_id`, `email`, `role`, `admin_status`.
  - Related: linked to both organisation and user action histories.

- Organisation Integration Details (`OrganisationIntegrationDetails`) — external provider connection mapping
  - Path: `src/domain/models/master_tables/organisation_integration_models.py`
  - Key fields: `connection_id` (PK; e.g., Nango), `organisation_id`, `integration_id`, `provider_config_key`.

Characteristics

- All use UUID primary keys and timestamp mixins (`created_at`, `updated_at`).
- These are global and not duplicated per tenant; they reference tenants via `organisation_id`.

## Organisation Tables (Tenant‑Scoped)

Located under `src/domain/models/organisation_tables`. These tables include `organisation_id` and store per‑tenant data.

### Chat

- Chat Sessions (`ChatSession`)
  - Path: `src/domain/models/organisation_tables/chat_session_models.py`
  - Keys: `session_id` (PK), `organisation_id`, `user_id` (creator), `chat_type`, `chat_version`.
  - Indexes: composite index on `(organisation_id, user_id)` for quick scoping.

- Chat Messages (`ChatMessage`)
  - Path: `src/domain/models/organisation_tables/chat_message_models.py`
  - Keys: `message_id` (PK), `session_id` (FK to session), optional `parent_message_id` for threads.
  - Metadata: `role`, `message_type`, `token_usage`, `model_name`, `agent_data`, `extra`.
  - Attachments: many‑to‑many via `ChatMessageAttachmentLink`, linking to `Attachment`.

- Attachments (`Attachment`)
  - Path: `src/domain/models/organisation_tables/chat_message_models.py`
  - Keys: `attachment_id` (PK), `organisation_id`, `user_id`, file metadata and processing `status`.

- Chat Jobs (`ChatMessageJob`)
  - Path: `src/domain/models/organisation_tables/job_models.py`
  - Keys: `job_id` (PK), `message_id` (FK), `job_type`, `status`, `job_metadata`, `result_data`.

Principle: Chat follows the generic “conversation + message” pattern. Sessions anchor ownership and scoping; messages are append‑only records with optional self‑reference for threads. Attachments use a many‑to‑many link table. Jobs are asynchronous processors tied to a message.

Tenancy and access

- All chat data is logically scoped by the session and organisation. Filter by `organisation_id` and `is_deleted` when applicable.

### Knowledge Base

- Sources (`OrganisationSource`)
  - Path: `src/domain/models/organisation_tables/knowledge_models.py`
  - Keys: `source_id` (PK), `organisation_id`, `user_id`, `source_type` (FILE or INTEGRATION), processing `status`.
  - For integrations: `integration_id` (FK to `KnowledgeIntegration`), `last_synced_at`.

- Knowledge Documents/Chunks (`OrganisationDocument`, table: `knowledge_base`)
  - Path: `src/domain/models/organisation_tables/knowledge_models.py`
  - Keys: `document_id` (PK), optional `source_id`, visibility flags `is_org_wide`, `uploader_user_id`.
  - Content: `content`, `embedding` (pgvector), `meta_data`, `filters`, `usage`, `content_hash`.
  - Indexes: visibility and deal filters; unique content hash for de‑duplication.

- Knowledge Jobs (`KnowledgeProcessingJob`)
  - Path: `src/domain/models/organisation_tables/job_models.py`
  - Keys: `job_id` (PK), `source_id` (FK), `job_type`, `status`, `job_metadata`, `result_data`.

Principle: Knowledge is modeled as “sources” (files or integrations) and “chunks/documents” for retrieval. Chunks carry embeddings (pgvector) and visibility flags. Jobs handle ingestion/extraction/embedding asynchronously.

Tenancy and access

- Always filter by `organisation_id` for sources and by visibility for documents (`is_org_wide` vs `uploader_user_id`).
- For LLM retrieval, combine vector similarity over `embedding` with filters (e.g., `deal_id`, tags, visibility flags).

### Integrations (Per‑Tenant)

- Knowledge Integrations (`KnowledgeIntegration`)
  - Path: `src/domain/models/organisation_tables/sync/integration_models.py`
  - Keys: `integration_id` (PK), `organisation_id`, `integration_type`, `integration_scope`, `created_by_user_id`, `is_active`.
  - Tokens/metadata: `credentials` (encrypted), connection identifiers, `scopes`, `connection_metadata`.

- Sync History (`IntegrationSyncHistory`)
  - Path: `src/domain/models/organisation_tables/sync/integration_models.py`
  - Keys: `sync_id` (PK), `integration_id` (FK), timestamps, `status`, counters, `error_details`.

Principle: An integration connection (credentials + metadata) owns sync history and any downstream synced entities. Provider-owned rows must be attributable to their source within the organisation.

Tenancy and access

- All integration records include `organisation_id`. Downstream synced tables reference `integration_id` and carry `organisation_id` via `SyncedRecordMixin`.

### Calendar (Synced Data)

- Shared mixin for synced records (`SyncedRecordMixin`)
  - Path: `src/domain/models/organisation_tables/sync/base.py`
  - Columns: `organisation_id`, `integration_id`, provider `external_id`, `data_payload`, `synced_at`, `source_modified_at`, `last_seen_at`, `is_deleted`.

- Events (`CalendarEvent`)
  - Path: `src/domain/models/organisation_tables/sync/calendar.py`
  - Keys: `event_id` (PK) + mixin columns, event fields (`title`, `description`, `start_at`, `end_at`, etc.).
  - Attendees: `CalendarAttendee` with `event_id` FK and uniqueness per event/email.

### CRM (Canonical Data With Source Mappings)

- CRM entity tables store canonical product state with local UUID primary keys and local foreign keys.
- Provider and migration identity is stored in `crm_source_object_mappings` as `(organisation_id, source_kind, integration_id/source_run_id, provider, external_object_type, external_id) -> (local_object_type, local_object_id)`.
- Raw provider payloads used for replay/debug belong on source mapping rows, not canonical CRM tables.
  This applies to deals, contacts, companies, users, pipelines, stages, activities, notes, associations, leads, and lead participants.

## IDs and Relationships Summary

- Tenancy
  - `organisation_id` — primary tenant key, present on all organisation tables and many integration‑related models.

- Identity
  - `user_id` — user primary key in master users; appears on many org tables for ownership/created‑by relations.
  - `supabase_user_id` — external auth identifier stored in master tables for mapping to the auth provider.

- Domain keys (non‑exhaustive)
  - Chat: `session_id`, `message_id`, `attachment_id`, `job_id`.
  - Knowledge: `source_id`, `document_id`, `job_id`.
  - Integrations & Sync: `integration_id`, `sync_id` and entity‑specific IDs (`event_id`, CRM deal/contact IDs). CRM provider IDs are resolved through source mappings.

## Query Patterns and Access Control

- Always scope tenant data by `organisation_id` and apply visibility flags (`is_org_wide`, `is_deleted`, `is_archived`).
- Prefer index‑supported filters:
  - Chat: `(organisation_id, user_id)`, `session_id`, `message_type`.
  - Knowledge: visibility, `deal_id`, `content_hash`, `embedding` for vector search.
  - Integrations: `(integration_id, started_at)` for sync history; `is_active` for connection state.
  - Calendar: `(organisation_id, start_at)` or `(integration_id, external_id)` depending on the entity.
  - CRM: local foreign-key lookups on canonical tables; provider lookup through `crm_source_object_mappings`.

## Conventions

- UUID primary keys; timestamps via `TimestampMixin` or `CreatedAtMixin`.
- JSONB fields for flexible metadata (`properties`, `tags`, `meta_data`, `filters`, `usage`, `connection_metadata`).
- Soft delete flags where applicable; do not hard‑delete production data by default.
- Enumerations for constrained values (e.g., chat roles/types, job status, integration scope/type).
- Vector embeddings via `pgvector` in the knowledge base for semantic retrieval.

## LLM Integration Notes

- Retrieval‑augmented generation (RAG): query `knowledge_base` by `organisation_id` with vector similarity on `embedding`, constrained by visibility and optional domain filters (`deal_id`, tags, message context).
- Context from chat: join recent `ChatMessage` rows in a `ChatSession` to provide conversational history.
- Integrations: use synced calendar tables and canonical CRM tables to enrich answers with up-to-date external context; for CRM provenance, resolve provider IDs and payloads through `crm_source_object_mappings`.

## Where to Find Things

- Master tables: `src/domain/models/master_tables`.
- Organisation tables: `src/domain/models/organisation_tables`.
- Sync (Calendar/CRM) and integration models: `src/domain/models/organisation_tables/sync`.

This document is intended to be the single reference for future LLMs and features that need a consistent understanding of our database layout and tenancy boundaries.

## Minimal, Reusable Table Templates

Use these as blueprints. Replace names/fields; keep the patterns.

### 1) Master table (global) template

```python
# Public schema, global scope, no organisation_id column
class MasterEntity(TimestampMixin, table=True):
  __tablename__ = "master_entities"
  __table_args__ = {"schema": "public", "comment": "Global master entity."}

  entity_id: UUID = Field(default_factory=uuid4, primary_key=True)
  # Stable typed fields
  name: str = Field(max_length=255, index=True)
  # Flexible metadata
  meta: dict[str, Any] = Field(sa_column=Column(JSONB, server_default=text("'{}'::jsonb")), default_factory=dict)
```

### 2) Organisation table (tenant‑scoped) template

```python
# Tenant scope: always include organisation_id + indexes for common filters
class OrgEntity(TimestampMixin, table=True):
  __tablename__ = "org_entities"
  __table_args__ = ({"comment": "Tenant‑scoped entity."},)

  entity_id: UUID = Field(default_factory=uuid4, primary_key=True)
  organisation_id: UUID = Field(index=True)
  # Optional ownership
  user_id: UUID | None = Field(default=None, index=True)

  # Business fields
  name: str = Field(max_length=255, index=True)
  properties: dict[str, Any] = Field(sa_column=Column(JSONB, server_default=text("'{}'::jsonb")), default_factory=dict)

  # Soft delete / archive
  is_deleted: bool = Field(default=False, sa_column=Column(Boolean, server_default=text("false"), nullable=False, index=True))
  is_archived: bool = Field(default=False, sa_column=Column(Boolean, server_default=text("false"), nullable=False, index=True))
```

### 3) Junction (many‑to‑many) template

```python
class EntityLink(SQLModel, table=True):
  __tablename__ = "entity_link"
  __table_args__ = ({"comment": "Junction table linking A ↔ B."},)

  a_id: UUID = Field(foreign_key="table_a.a_id", primary_key=True)
  b_id: UUID = Field(foreign_key="table_b.b_id", primary_key=True)
  # Optional metadata
  order: int = Field(default=0)
```

### 4) Synced record (integration) template

```python
class SyncedEntity(SyncedRecordMixin, table=True):
  __tablename__ = "synced_entities"
  __table_args__ = ({"comment": "Provider‑synced record."},)

  entity_id: UUID = Field(default_factory=uuid4, primary_key=True)
  # Domain fields
  title: str | None = Field(default=None, index=True)
```

## Naming + Constraint Patterns (copy/paste)

- Tables: snake_case plural (e.g., `chat_messages`, `knowledge_integrations`).
- PKs: `<entity>_id` (UUID).
- FKs: `fk_<child>_<parent>_<col>` when manually named in migrations.
- Unique: `uq_<table>_<cols>`.
- Indexes: `ix_<table>_<col1>[_<col2>]` with composite order matching query patterns.
- Check constraints: `ck_<table>_<rule>`.

## Quick Checklist (design a new table)

1) Scope: master (global) or organisation (tenant‑scoped)? If tenant‑scoped, add `organisation_id` and index it.
2) Keys: UUID PK; define required FKs with clear delete behavior (CASCADE vs SET NULL).
3) Auditing: include `created_at`, `updated_at`. Add history table only when necessary.
4) Deletion: soft delete flags for user‑visible entities; avoid hard deletes.
5) Flexibility: move volatile/optional fields into JSONB.
6) Indexes: FKs, frequent filters, and composite indexes for common queries.
7) Visibility: if needed, add `is_org_wide` vs owner `user_id` pattern.
8) Sync: for non-CRM provider data, inherit from `SyncedRecordMixin` and ensure `(integration_id, external_id)` uniqueness. For canonical CRM tables, use local IDs on the entity row and store provider identity in `crm_source_object_mappings`.
9) Migrations: add comments, names for constraints, and reversible `downgrade()`.
10) Tests: basic CRUD + RLS/tenant scoping where applicable.
