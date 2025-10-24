# Organisation Database Schema

Practical guidance for designing tenant‑scoped (organisation) tables. Use these patterns to create microservice tables that integrate cleanly across the platform.

## Tenancy (must‑have)

- Always include `organisation_id` (UUID) and index it.
- Add `user_id` when ownership or actor is relevant.
- Prefer soft deletes (`is_deleted`, optional `deleted_at`) for user‑visible entities.
- Include timestamps via `TimestampMixin` for auditable changes.

## Core Patterns

### 1) Tenant entity

Principle: Stable columns as typed fields; evolving/optional data in JSONB. Visibility and retention are explicit flags.

Typical columns:
- `entity_id` (UUID PK)
- `organisation_id` (UUID, indexed)
- Optional `user_id` (owner/actor)
- Business fields (typed)
- `properties`/`metadata` (JSONB)
- `is_deleted`, `is_archived` (BOOLEAN, indexed)
- `created_at`, `updated_at`

### 2) Conversation + message (chat‑like) pattern

Principle: A session anchors scope; messages are append‑only with optional parent for threads; attachments via many‑to‑many link table; jobs are async processors tied to messages.

Tables involved:
- Sessions: `session_id`, `organisation_id`, `user_id`, metadata, soft delete
- Messages: `message_id`, `session_id` FK, optional `parent_message_id`, role/type, content JSONB
- Attachments: central `attachments` + junction table for message ↔ attachment
- Jobs: background records linked to a message

Use this pattern for any conversational or event‑stream use case.

### 3) Knowledge retrieval pattern

Principle: Separate original “sources” from chunked “documents”. Chunks carry embeddings and visibility; jobs orchestrate processing.

Tables involved:
- Sources: `source_id`, `organisation_id`, `user_id`, type (`file`/`integration`), status, storage or integration FK
- Documents (chunks): `document_id`, `source_id`, `is_org_wide` vs `uploader_user_id`, `embedding` (pgvector), metadata/filters
- Jobs: extraction/chunking/embedding pipeline

Use this for any content you want to search semantically.

### 4) Integration sync pattern

Principle: A connection owns sync history and downstream synced rows. All synced rows must be uniquely identified by `(integration_id, external_id)` and carry `organisation_id`.

Tables/mixins:
- `knowledge_integrations` (connection): credentials, scope/type, state
- `integration_sync_history`: runs/counters/status
- `SyncedRecordMixin`: common columns for synced tables → `organisation_id`, `integration_id`, `external_id`, `data_payload`, `synced_at`, `source_modified_at`, `last_seen_at`, `is_deleted`

Apply this to Calendar, CRM, or any provider data.

### 5) Junction (many‑to‑many) pattern

Principle: Use a dedicated link table with composite PK of both FKs; add metadata (e.g., order) if needed; cascade deletes.

## Minimal Templates

Use these as starting points—rename and adjust fields.

```python
class OrgEntity(TimestampMixin, table=True):
    __tablename__ = "org_entities"
    entity_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    user_id: UUID | None = Field(default=None, index=True)
    name: str = Field(max_length=255, index=True)
    metadata: dict[str, Any] = Field(sa_column=Column(JSONB, server_default=text("'{}'::jsonb")), default_factory=dict)
    is_deleted: bool = Field(default=False, sa_column=Column(Boolean, server_default=text("false"), nullable=False, index=True))
    is_archived: bool = Field(default=False, sa_column=Column(Boolean, server_default=text("false"), nullable=False, index=True))
```

```python
class EntityLink(SQLModel, table=True):
    __tablename__ = "entity_link"
    a_id: UUID = Field(foreign_key="table_a.a_id", primary_key=True)
    b_id: UUID = Field(foreign_key="table_b.b_id", primary_key=True)
    order: int = Field(default=0)
```

```python
class SyncedEntity(SyncedRecordMixin, table=True):
    __tablename__ = "synced_entities"
    entity_id: UUID = Field(default_factory=uuid4, primary_key=True)
    title: str | None = Field(default=None, index=True)
```

## Design Checklist (Organisation)

- [ ] Include `organisation_id` (indexed) and soft delete flags where appropriate
- [ ] Define FKs with correct `ondelete` behavior (CASCADE vs SET NULL)
- [ ] Add composite indexes for common filters (e.g., `(organisation_id, created_at)`)
- [ ] Push volatile fields to JSONB; keep critical fields typed
- [ ] For visibility: choose `is_org_wide` vs owner `user_id` pattern
- [ ] For synced data: ensure `(integration_id, external_id)` uniqueness and provenance
- [ ] Add table/comment strings for clarity in migrations
- [ ] Keep migrations reversible and constrain naming (`ix_`, `uq_`, `ck_` prefixes)

## Domain Pointers (no deep dives)

- Chat: conversation + message pattern; attachments via junction; jobs for async steps.
- Knowledge: source → chunked document with embeddings; jobs for ingestion.
- Integrations/Sync: connection + history + synced tables via `SyncedRecordMixin`.
- Calendar/CRM: concrete examples of the sync pattern; avoid domain‑specific columns unless essential.

See `database-architecture.md` for big‑picture context.

