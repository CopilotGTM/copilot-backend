# Adding New CRM Entities - Developer Guide

This guide explains how to add a new CRM entity using the new modular architecture.

## Architecture Overview

The new architecture separates concerns into focused, testable components:

- **Service Layer** (`src/service/integrations/crm/entities/`): Domain logic for transforming and persisting data
- **Worker Layer** (`src/workers/integrations_worker/`): Infrastructure for fetching data from APIs
- **Adapters** (`src/workers/integrations_worker/adapters/`): Glue between fetchers and services

## Example: Owner Entity

The Owner entity demonstrates the pattern. Use it as a reference when adding new entities.

## Step-by-Step Guide

### 1. Define the Domain Model (if not exists)

**Location**: `src/domain/models/organisation_tables/sync/crm/`

```python
# Example: owners.py
from sqlmodel import Field, SQLModel

class CRMOwner(SyncedRecordMixin, table=True):
    __tablename__ = "crm_owners"
    
    owner_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_id: str = Field(sa_column=Column(String(255), nullable=False))
    # ... other fields
```

### 2. Create Sync Service

**Location**: `src/service/integrations/crm/entities/<entity>_sync_service.py`

```python
from src.service.integrations.core import BaseSyncService
from src.domain.models.organisation_tables.sync.crm import CRMOwner

class OwnerSyncService(BaseSyncService[CRMOwner]):
    """Service for syncing owner data."""
    
    async def sync(
        self, 
        *, 
        integration: KnowledgeIntegration,
        records: Iterable[dict],
        provider: str,
    ) -> dict[str, Any]:
        # Initialize tracking
        result = self._init_result()
        
        # Load existing records
        existing = await self._load_existing_by_external_ids(...)
        
        # Process each record
        for record in records:
            # Transform and save
            ...
        
        # Commit and return results
        await self.db.commit()
        return result.to_dict()
```

**Key Methods**:
- `sync()`: Main entry point
- `_transform_record()`: Convert provider data to domain model
- `_update_record()`: Update existing records with new data

### 3. Create Provider Fetcher

**Location**: `src/workers/integrations_worker/providers/crm/hubspot/fetchers/<entity>_fetcher.py`

```python
from src.workers.integrations_worker.core import BaseFetcher

class HubSpotOwnerFetcher(BaseFetcher):
    """Fetches owners from HubSpot API."""
    
    async def fetch_all(self, *, incremental: bool = False) -> list[dict]:
        endpoint = "/crm/v3/owners"
        params = {"limit": 100}
        
        return await self._fetch_paginated(endpoint, params)
```

**Key Methods**:
- `fetch_all()`: Fetch all records (with optional incremental filtering)
- `fetch_by_ids()`: Fetch specific records (optional)
- `_extract_records_from_response()`: Parse API response
- `_has_next_page()`: Check pagination
- `_get_next_page_params()`: Get next page cursor

### 4. Create Entity Adapter

**Location**: `src/workers/integrations_worker/adapters/crm/entity_adapters/<entity>_adapter.py`

```python
from src.service.integrations.crm.entities import OwnerSyncService

async def sync_owners(
    *,
    service: OwnerSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict],
    provider: str,
) -> dict[str, Any]:
    """Sync owner records."""
    return await service.sync(
        integration=integration,
        records=records,
        provider=provider,
    )
```

### 5. Register in CRM Adapter

**Location**: `src/workers/integrations_worker/adapters/crm_adapter.py`

Add import and handler:

```python
from src.service.integrations.crm.entities import OwnerSyncService
from src.workers.integrations_worker.adapters.crm.entity_adapters import sync_owners

async def sync_crm_data(...):
    if data_type == "owners" and isinstance(service, OwnerSyncService):
        return await sync_owners(...)
```

### 6. Create Celery Task (Optional)

**Location**: `src/workers/integrations_worker/tasks/crm/sync_<entity>.py`

```python
from src.workers.integrations_worker.celery_app import app

@app.task(name="integrations.crm.sync_owners")
def sync_owners_task(organisation_id: str, integration_id: str):
    # Task implementation
    pass
```

### 7. Update __init__ files

Add exports to:
- `src/service/integrations/crm/entities/__init__.py`
- `src/workers/integrations_worker/providers/crm/hubspot/fetchers/__init__.py`
- `src/workers/integrations_worker/adapters/crm/entity_adapters/__init__.py`

## Testing

### Unit Tests

Test each component independently:

```python
# Test sync service
async def test_owner_sync_service():
    service = OwnerSyncService(db, org_id)
    result = await service.sync(integration, records, "hubspot")
    assert result["records_processed"] > 0

# Test fetcher
async def test_hubspot_owner_fetcher():
    fetcher = HubSpotOwnerFetcher(nango_client, config_key, conn_id)
    owners = await fetcher.fetch_all()
    assert len(owners) > 0

# Test adapter
async def test_sync_owners_adapter():
    result = await sync_owners(
        service=service,
        integration=integration,
        records=mock_records,
        provider="hubspot"
    )
    assert "records_processed" in result
```

### Integration Tests

Test end-to-end flow:

```python
async def test_owner_sync_integration():
    # 1. Fetch from API
    fetcher = HubSpotOwnerFetcher(...)
    records = await fetcher.fetch_all()
    
    # 2. Sync to database
    service = OwnerSyncService(db, org_id)
    result = await service.sync(integration, records, "hubspot")
    
    # 3. Verify persistence
    owners = await db.execute(select(CRMOwner))
    assert owners.count() > 0
```

## Best Practices

### Service Layer

1. **Keep services focused**: One entity type per service
2. **Use result tracking**: Leverage `SyncResult` for consistent reporting
3. **Handle errors gracefully**: Log and track failed records
4. **Validate data**: Check required fields before persisting
5. **Batch operations**: Use bulk inserts/updates when possible

### Fetcher Layer

1. **Implement pagination**: Always handle multi-page responses
2. **Respect rate limits**: Use exponential backoff
3. **Log progress**: Track pages fetched and records retrieved
4. **Handle API errors**: Catch and log provider-specific errors
5. **Support incremental**: Implement filtering by update date when possible

### Adapter Layer

1. **Keep adapters thin**: Just delegate to service
2. **Type hints**: Use proper type annotations
3. **Documentation**: Explain any entity-specific behavior

## Checklist for New Entity

- [ ] Domain model created/exists
- [ ] Sync service implemented
- [ ] Provider fetcher(s) implemented
- [ ] Entity adapter created
- [ ] Registered in CRM adapter
- [ ] Celery task created (if needed)
- [ ] __init__ files updated
- [ ] Unit tests added
- [ ] Integration tests added
- [ ] Documentation updated

## Common Patterns

### Loading Relationships

```python
# In sync service
async def _load_related_entities(self, entity_ids: list[str]):
    stmt = select(RelatedEntity).where(
        RelatedEntity.external_id.in_(entity_ids)
    )
    result = await self.db.execute(stmt)
    return {row.external_id: row for row in result.scalars()}
```

### Soft Deletes

```python
# Mark records not in latest sync as deleted
for ext_id, record in existing.items():
    if ext_id not in seen_external_ids:
        record.is_deleted = True
        record.last_seen_at = now
        self.db.add(record)
```

### Provider-Specific Logic

```python
# In sync service
def _update_record(self, record, raw, provider):
    # Common fields
    record.name = raw.get("name")
    
    # Provider-specific
    if provider == "hubspot":
        record.hs_specific_field = raw.get("hsField")
    elif provider == "salesforce":
        record.sf_specific_field = raw.get("sfField")
```

## Migration from Monolithic Service

When migrating existing entities from `crm_sync_service.py`:

1. Extract the sync logic into new service class
2. Create corresponding fetcher
3. Create adapter
4. Update dispatcher to support both old and new pattern
5. Add tests
6. Verify in staging
7. Deploy
8. Remove old code after validation

## Questions?

Refer to the Owner entity implementation as a complete example:
- `src/service/integrations/crm/entities/owner_sync_service.py`
- `src/workers/integrations_worker/providers/crm/hubspot/fetchers/owner_fetcher.py`
- `src/workers/integrations_worker/adapters/crm/entity_adapters/owner_adapter.py`
