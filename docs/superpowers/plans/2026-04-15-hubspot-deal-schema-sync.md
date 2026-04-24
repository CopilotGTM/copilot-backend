# HubSpot Deal Schema Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the reusable CRM schema-sync framework and implement the first live slice: HubSpot deal property schema fetch + normalization + persistence into the provider-backed field registry, without syncing deal records yet.

**Architecture:** Use the shared `crm_custom_field_definitions` registry table for both internal and provider-backed field definitions, but harden all existing internal custom-column reads to `field_source=internal` before writing provider rows. Place provider-agnostic schema logic under `src/service/integrations/crm/common/`, HubSpot schema orchestration under `src/service/integrations/crm/schema/`, and HubSpot API fetch logic under `src/workers/integrations_worker/providers/crm/hubspot/`.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy async, PostgreSQL JSONB, httpx, pytest, Ruff

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `src/service/integrations/crm/__init__.py` | Package marker/export for CRM integration services |
| `src/service/integrations/crm/common/__init__.py` | Package marker/export for CRM schema common types/services |
| `src/service/integrations/crm/common/schema_types.py` | Provider-neutral dataclasses for normalized schema payloads |
| `src/service/integrations/crm/common/field_type_mapper.py` | Map HubSpot property metadata to `CRMCustomFieldDataType` |
| `src/service/integrations/crm/common/field_registry_service.py` | Upsert/read/deactivate provider-backed rows in `CRMCustomFieldDefinition` |
| `src/service/integrations/crm/schema/__init__.py` | Package marker/export for schema sync services |
| `src/service/integrations/crm/schema/schema_sync_result.py` | Typed result object for schema sync counters |
| `src/service/integrations/crm/schema/hubspot_deal_schema_service.py` | Orchestrate token resolution, fetch, normalize, registry write |
| `src/workers/integrations_worker/providers/crm/__init__.py` | Real CRM provider package marker replacing pycache-only ghost package |
| `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` | HubSpot CRM provider package marker |
| `src/workers/integrations_worker/providers/crm/hubspot/deal_schema_fetcher.py` | Fetch HubSpot deal property definitions from HubSpot API |
| `tests/service/crm/internal/test_custom_field_source_filtering.py` | Internal-service tests proving provider rows are excluded |
| `tests/api/routers/crm/test_custom_fields_handler.py` | Handler tests for internal-only custom-column listing |
| `tests/service/integrations/crm/common/test_field_type_mapper.py` | Unit tests for HubSpot → internal type mapping |
| `tests/service/integrations/crm/common/test_field_registry_service.py` | Service tests for provider-backed field registry writes |
| `tests/service/integrations/crm/schema/test_hubspot_deal_schema_service.py` | Orchestration tests for HubSpot schema sync |
| `tests/workers/integrations_worker/providers/crm/hubspot/test_deal_schema_fetcher.py` | Fetcher tests for HubSpot schema API pagination/shape |

### Modified Files

| File | Change |
|------|--------|
| `src/domain/models/organisation_tables/sync/crm/custom_fields.py` | Already extended in this branch; use new source/provenance columns in implementation |
| `src/service/crm/internal/custom_field_service.py` | Filter reads to `field_source=internal` |
| `src/service/crm/internal/custom_field_write_service.py` | Filter reads/writes/uniqueness checks to `field_source=internal` |
| `src/service/crm/internal/deal_write_service.py` | Validate custom properties against internal-only field definitions |
| `src/api/routers/crm/custom_fields_handler.py` | No behavioral change expected, but covered by internal-only tests |
| `src/api/routers/crm/deals_handler.py` | Filter deal custom-column definitions/keys to `field_source=internal` |
| `src/api/routers/crm/deal_import_handler.py` | Filter import-side custom-field lookups to `field_source=internal` |
| `docs/Technical Documentation/integrations/architecture-overview.md` | Add CRM schema-sync subtree and HubSpot deal schema sync |
| `docs/Technical Documentation/integrations/sync-by-provider.md` | Document HubSpot deal-schema sync as implemented before record sync |
| `docs/API Documentation/deals/crm-custom-fields-api.md` | Clarify shared registry table, internal-only API reads, provider rows excluded |

---

## Task 1: Harden Internal Custom-Field Reads To `field_source=internal`

**Files:**
- Create: `tests/service/crm/internal/test_custom_field_source_filtering.py`
- Create: `tests/api/routers/crm/test_custom_fields_handler.py`
- Modify: `src/service/crm/internal/custom_field_service.py`
- Modify: `src/service/crm/internal/custom_field_write_service.py`
- Modify: `src/service/crm/internal/deal_write_service.py`
- Modify: `src/api/routers/crm/deals_handler.py`
- Modify: `src/api/routers/crm/deal_import_handler.py`

- [ ] **Step 1: Write the failing internal-service tests**

```python
async def test_list_fields_excludes_provider_backed_rows(session, organisation_id, user_id):
    internal = CRMCustomFieldDefinition(
        organisation_id=organisation_id,
        entity_type=CRMCustomFieldEntity.DEAL,
        field_source=CRMCustomFieldSource.INTERNAL,
        key="priority",
        label="Priority",
        data_type=CRMCustomFieldDataType.TEXT,
    )
    provider = CRMCustomFieldDefinition(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        entity_type=CRMCustomFieldEntity.DEAL,
        field_source=CRMCustomFieldSource.PROVIDER,
        provider_name="hubspot",
        key="dealstage",
        label="Deal Stage",
        data_type=CRMCustomFieldDataType.SELECT,
    )
    session.add_all([internal, provider])
    await session.flush()

    service = CustomFieldWriteService(session, organisation_id=organisation_id, user_id=user_id)
    rows = await service.list_fields(entity=CRMCustomFieldEntity.DEAL)

    assert [row.key for row in rows] == ["priority"]
```

```python
def test_custom_fields_handler_lists_internal_columns_only(client, deals_dependency_overrides):
    response = client.get("/users/crm/custom-fields")
    assert response.status_code == 200
    keys = [item["key"] for item in response.json()["data"]]
    assert "priority" in keys
    assert "dealstage" not in keys
```

- [ ] **Step 2: Run the focused tests to verify they fail for the correct reason**

Run:
```bash
uv run pytest tests/service/crm/internal/test_custom_field_source_filtering.py tests/api/routers/crm/test_custom_fields_handler.py -q
```

Expected:
- FAIL because current queries only filter by `organisation_id/entity_type/is_deleted/is_active`
- provider-backed rows leak into internal results

- [ ] **Step 3: Add `field_source=internal` filters to the internal services and handlers**

```python
from src.domain.models.organisation_tables.sync.crm.custom_fields import CRMCustomFieldSource

stmt = (
    select(CRMCustomFieldDefinition)
    .where(
        col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
        col(CRMCustomFieldDefinition.entity_type) == entity.value,
        col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
        col(CRMCustomFieldDefinition.is_deleted).is_(False),
    )
)
```

```python
stmt = (
    select(col(CRMCustomFieldDefinition.key))
    .where(
        col(CRMCustomFieldDefinition.organisation_id) == organisation_id,
        col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
        col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
        col(CRMCustomFieldDefinition.is_deleted).is_(False),
        col(CRMCustomFieldDefinition.is_active).is_(True),
    )
)
```

- [ ] **Step 4: Run the tests to verify the internal paths are isolated**

Run:
```bash
uv run pytest tests/service/crm/internal/test_custom_field_source_filtering.py tests/api/routers/crm/test_custom_fields_handler.py tests/api/routers/crm/test_deals_handler.py -q
```

Expected:
- PASS
- internal custom-column responses contain only `field_source=internal` rows

- [ ] **Step 5: Commit**

```bash
git add tests/service/crm/internal/test_custom_field_source_filtering.py tests/api/routers/crm/test_custom_fields_handler.py src/service/crm/internal/custom_field_service.py src/service/crm/internal/custom_field_write_service.py src/service/crm/internal/deal_write_service.py src/api/routers/crm/deals_handler.py src/api/routers/crm/deal_import_handler.py
git commit -m "fix: isolate internal custom field reads by source"
```

---

## Task 2: Create CRM Schema Common Types And Type Mapper

**Files:**
- Create: `src/service/integrations/crm/__init__.py`
- Create: `src/service/integrations/crm/common/__init__.py`
- Create: `src/service/integrations/crm/common/schema_types.py`
- Create: `src/service/integrations/crm/common/field_type_mapper.py`
- Create: `tests/service/integrations/crm/common/test_field_type_mapper.py`

- [ ] **Step 1: Write the failing mapper tests**

```python
def test_map_hubspot_checkbox_to_boolean() -> None:
    raw = {"type": "bool", "fieldType": "booleancheckbox"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.BOOLEAN


def test_map_hubspot_enumeration_booleancheckbox_to_select() -> None:
    raw = {"type": "enumeration", "fieldType": "booleancheckbox"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.SELECT


def test_map_hubspot_enumeration_to_select() -> None:
    raw = {"type": "enumeration", "fieldType": "select"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.SELECT


def test_map_hubspot_calculation_equation_to_formula() -> None:
    raw = {"type": "number", "fieldType": "calculation_equation"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.FORMULA


def test_map_unknown_type_to_text() -> None:
    raw = {"type": "object_coordinates", "fieldType": "text"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.TEXT
```

- [ ] **Step 2: Run the mapper tests to verify they fail**

Run:
```bash
uv run pytest tests/service/integrations/crm/common/test_field_type_mapper.py -q
```

Expected:
- FAIL because `field_type_mapper.py` and exported symbols do not exist yet

- [ ] **Step 3: Create provider-neutral schema dataclasses**

```python
@dataclass(slots=True)
class NormalizedFieldOption:
    value: str
    label: str
    display_order: int | None = None
    is_hidden: bool = False


@dataclass(slots=True)
class NormalizedCRMFieldDefinition:
    entity_type: CRMCustomFieldEntity
    key: str
    label: str
    data_type: CRMCustomFieldDataType
    description: str | None
    required: bool
    is_provider_custom: bool | None
    external_id: str | None
    source_modified_at: datetime | None
    options: list[NormalizedFieldOption]
    data_payload: dict[str, Any]
```

- [ ] **Step 4: Implement the HubSpot field-type mapper**

```python
def map_hubspot_field_type(raw: Mapping[str, Any]) -> CRMCustomFieldDataType:
    prop_type = str(raw.get("type") or "").lower()
    field_type = str(raw.get("fieldType") or "").lower()

    if field_type == "calculation_equation":
        return CRMCustomFieldDataType.FORMULA
    if prop_type == "bool":
        return CRMCustomFieldDataType.BOOLEAN
    if prop_type == "date":
        return CRMCustomFieldDataType.DATE
    if prop_type == "datetime":
        return CRMCustomFieldDataType.DATETIME
    if prop_type == "number":
        return CRMCustomFieldDataType.NUMBER
    if prop_type == "enumeration" and field_type in {"checkbox", "multiple_checkboxes"}:
        return CRMCustomFieldDataType.MULTISELECT
    if prop_type == "enumeration":
        return CRMCustomFieldDataType.SELECT
    return CRMCustomFieldDataType.TEXT
```

- [ ] **Step 5: Run the mapper tests to verify they pass**

Run:
```bash
uv run pytest tests/service/integrations/crm/common/test_field_type_mapper.py -q
```

Expected:
- PASS

- [ ] **Step 6: Commit**

```bash
git add src/service/integrations/crm/__init__.py src/service/integrations/crm/common/__init__.py src/service/integrations/crm/common/schema_types.py src/service/integrations/crm/common/field_type_mapper.py tests/service/integrations/crm/common/test_field_type_mapper.py
git commit -m "feat: add CRM schema common types and hubspot field mapper"
```

---

## Task 3: Implement Provider Field Registry Service

**Files:**
- Create: `src/service/integrations/crm/common/field_registry_service.py`
- Create: `tests/service/integrations/crm/common/test_field_registry_service.py`

- [ ] **Step 1: Write the failing registry tests**

```python
async def test_upsert_provider_fields_creates_provider_backed_rows(session, organisation_id, integration_id):
    service = FieldRegistryService(session, organisation_id=organisation_id, integration_id=integration_id)
    fields = [
        NormalizedCRMFieldDefinition(
            entity_type=CRMCustomFieldEntity.DEAL,
            key="dealstage",
            label="Deal Stage",
            data_type=CRMCustomFieldDataType.SELECT,
            description="HubSpot deal stage",
            required=False,
            is_provider_custom=False,
            external_id=None,
            source_modified_at=None,
            options=[],
            data_payload={"name": "dealstage"},
        )
    ]

    result = await service.upsert_provider_fields(provider_name="hubspot", fields=fields)

    assert result.created == 1
```

```python
async def test_upsert_provider_fields_marks_missing_rows_inactive(session, organisation_id, integration_id):
    existing = CRMCustomFieldDefinition(
        organisation_id=organisation_id,
        integration_id=integration_id,
        entity_type=CRMCustomFieldEntity.DEAL,
        field_source=CRMCustomFieldSource.PROVIDER,
        provider_name="hubspot",
        key="legacy_field",
        label="Legacy Field",
        data_type=CRMCustomFieldDataType.TEXT,
    )
    session.add(existing)
    await session.flush()

    service = FieldRegistryService(session, organisation_id=organisation_id, integration_id=integration_id)
    result = await service.upsert_provider_fields(
        provider_name="hubspot",
        fields=[],
        entity_types=[CRMCustomFieldEntity.DEAL],
    )

    assert result.marked_inactive == 1
```

- [ ] **Step 2: Run the registry tests to verify they fail**

Run:
```bash
uv run pytest tests/service/integrations/crm/common/test_field_registry_service.py -q
```

Expected:
- FAIL because `FieldRegistryService` does not exist yet

- [ ] **Step 3: Implement the provider registry write path**

```python
class FieldRegistryService:
    def __init__(self, session: AsyncSession, *, organisation_id: UUID, integration_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id

    async def upsert_provider_fields(
        self,
        *,
        provider_name: str,
        fields: Sequence[NormalizedCRMFieldDefinition],
        entity_types: Sequence[CRMCustomFieldEntity] | None = None,
    ) -> SchemaSyncResult:
        existing = await self._load_existing_provider_fields(entity_type=CRMCustomFieldEntity.DEAL)
        existing_by_key = {row.key: row for row in existing}
        seen_keys: set[str] = set()
        result = SchemaSyncResult(fetched=len(fields))

        for field in fields:
            seen_keys.add(field.key)
            row = existing_by_key.get(field.key)
            if row is None:
                self.session.add(self._build_row(provider_name=provider_name, field=field))
                result.created += 1
                continue
            changed = self._apply_updates(row=row, field=field)
            result.updated += int(changed)
            result.unchanged += int(not changed)

        for row in existing:
            if row.key not in seen_keys and row.is_active:
                row.is_active = False
                result.marked_inactive += 1

        await self.session.flush()
        return result
```

Notes:
- This method applies full-snapshot semantics only within the targeted entity scope.
- When `fields=[]`, callers must pass `entity_types=[...]` explicitly so missing-row deactivation
  cannot accidentally apply to every provider entity in the integration.

- [ ] **Step 4: Run the registry tests to verify they pass**

Run:
```bash
uv run pytest tests/service/integrations/crm/common/test_field_registry_service.py -q
```

Expected:
- PASS

- [ ] **Step 5: Commit**

```bash
git add src/service/integrations/crm/common/field_registry_service.py tests/service/integrations/crm/common/test_field_registry_service.py
git commit -m "feat: add provider field registry service"
```

---

## Task 4: Add HubSpot Deal Schema Fetcher

**Files:**
- Create: `src/workers/integrations_worker/providers/crm/__init__.py`
- Create: `src/workers/integrations_worker/providers/crm/hubspot/__init__.py`
- Create: `src/workers/integrations_worker/providers/crm/hubspot/deal_schema_fetcher.py`
- Create: `tests/workers/integrations_worker/providers/crm/hubspot/test_deal_schema_fetcher.py`

- [ ] **Step 1: Write the failing fetcher tests**

```python
@pytest.mark.asyncio
async def test_fetch_deal_schema_returns_results_from_hubspot_properties_api(respx_mock):
    respx_mock.get("https://api.hubapi.com/crm/v3/properties/deals").mock(
        return_value=httpx.Response(200, json={"results": [{"name": "dealstage", "label": "Deal Stage"}]})
    )

    fetcher = HubSpotDealSchemaFetcher(access_token="token")
    rows = await fetcher.fetch()

    assert rows == [{"name": "dealstage", "label": "Deal Stage"}]
```

```python
@pytest.mark.asyncio
async def test_fetch_deal_schema_follows_paging_next_link(respx_mock):
    respx_mock.get("https://api.hubapi.com/crm/v3/properties/deals").mock(
        side_effect=[
            httpx.Response(200, json={"results": [{"name": "a"}], "paging": {"next": {"after": "1"}}}),
            httpx.Response(200, json={"results": [{"name": "b"}]}),
        ]
    )

    rows = await HubSpotDealSchemaFetcher(access_token="token").fetch()
    assert [row["name"] for row in rows] == ["a", "b"]
```

- [ ] **Step 2: Run the fetcher tests to verify they fail**

Run:
```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_deal_schema_fetcher.py -q
```

Expected:
- FAIL because the fetcher module does not exist yet

- [ ] **Step 3: Implement the fetcher with OAuth token + 429 retry**

```python
class HubSpotDealSchemaFetcher:
    BASE_URL = "https://api.hubapi.com/crm/v3/properties/deals"

    def __init__(self, *, access_token: str, timeout: float = 30.0):
        self.access_token = access_token
        self.timeout = timeout

    async def fetch(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        after: str | None = None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            while True:
                response = await self._get_page(client=client, after=after)
                payload = response.json()
                results.extend(payload.get("results") or [])
                after = ((payload.get("paging") or {}).get("next") or {}).get("after")
                if not after:
                    return results
```

- [ ] **Step 4: Run the fetcher tests to verify they pass**

Run:
```bash
uv run pytest tests/workers/integrations_worker/providers/crm/hubspot/test_deal_schema_fetcher.py -q
```

Expected:
- PASS

- [ ] **Step 5: Commit**

```bash
git add src/workers/integrations_worker/providers/crm/__init__.py src/workers/integrations_worker/providers/crm/hubspot/__init__.py src/workers/integrations_worker/providers/crm/hubspot/deal_schema_fetcher.py tests/workers/integrations_worker/providers/crm/hubspot/test_deal_schema_fetcher.py
git commit -m "feat: add hubspot deal schema fetcher"
```

---

## Task 5: Implement HubSpot Deal Schema Sync Service

**Files:**
- Create: `src/service/integrations/crm/schema/__init__.py`
- Create: `src/service/integrations/crm/schema/schema_sync_result.py`
- Create: `src/service/integrations/crm/schema/hubspot_deal_schema_service.py`
- Create: `tests/service/integrations/crm/schema/test_hubspot_deal_schema_service.py`

- [ ] **Step 1: Write the failing orchestration tests**

```python
@pytest.mark.asyncio
async def test_sync_hubspot_deal_schema_creates_provider_rows(mocker):
    integration = SimpleNamespace(
        integration_id=uuid4(),
        organisation_id=uuid4(),
        integration_type=IntegrationType.HUBSPOT,
        credentials={"access_token": "token", "refresh_token": "refresh"},
        access_token_expires_at=None,
    )
    mocker.patch(
        "src.service.integrations.crm.schema.hubspot_deal_schema_service.get_access_token",
        return_value="token",
    )
    mocker.patch(
        "src.service.integrations.crm.schema.hubspot_deal_schema_service.HubSpotDealSchemaFetcher.fetch",
        return_value=[{"name": "dealstage", "label": "Deal Stage", "type": "enumeration", "fieldType": "select"}],
    )

    result = await HubSpotDealSchemaService(...).sync(integration)

    assert result.fetched == 1
    assert result.created == 1
```

- [ ] **Step 2: Run the orchestration tests to verify they fail**

Run:
```bash
uv run pytest tests/service/integrations/crm/schema/test_hubspot_deal_schema_service.py -q
```

Expected:
- FAIL because the schema service/result types do not exist yet

- [ ] **Step 3: Create the typed result object**

```python
@dataclass(slots=True)
class SchemaSyncResult:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    marked_inactive: int = 0
    failed: int = 0
```

- [ ] **Step 4: Implement the HubSpot schema orchestration service**

```python
class HubSpotDealSchemaService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def sync(self, integration: KnowledgeIntegration) -> SchemaSyncResult:
        token_info = IntegrationTokenInfo.from_integration(integration, self.organisation_id)
        access_token = await get_access_token(token_info)

        fetcher = HubSpotDealSchemaFetcher(access_token=access_token)
        raw_fields = await fetcher.fetch()

        normalized = [self._normalize_field(raw) for raw in raw_fields]

        registry = FieldRegistryService(
            self.session,
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
        )
        result = await registry.upsert_provider_fields(provider_name="hubspot", fields=normalized)
        await self.session.commit()
        return result
```

- [ ] **Step 5: Run the orchestration tests to verify they pass**

Run:
```bash
uv run pytest tests/service/integrations/crm/schema/test_hubspot_deal_schema_service.py tests/service/integrations/crm/common/test_field_registry_service.py tests/workers/integrations_worker/providers/crm/hubspot/test_deal_schema_fetcher.py -q
```

Expected:
- PASS

- [ ] **Step 6: Commit**

```bash
git add src/service/integrations/crm/schema/__init__.py src/service/integrations/crm/schema/schema_sync_result.py src/service/integrations/crm/schema/hubspot_deal_schema_service.py tests/service/integrations/crm/schema/test_hubspot_deal_schema_service.py
git commit -m "feat: add hubspot deal schema sync service"
```

---

## Task 6: Verify Internal And Provider Paths Coexist Correctly

**Files:**
- Modify: `tests/api/routers/crm/test_deals_handler.py`
- Modify: `tests/service/integrations/crm/common/test_field_registry_service.py`
- Modify: `tests/service/crm/internal/test_custom_field_source_filtering.py`

- [ ] **Step 1: Add a regression test that internal deal detail ignores provider rows**

```python
def test_get_deal_includes_only_internal_custom_column_definitions(client, deals_dependency_overrides):
    response = client.get(f"/users/crm/deals/{INTERNAL_DEAL_ID}")
    assert response.status_code == 200

    definitions = response.json()["data"]["custom_column_definitions"]
    keys = [item["key"] for item in definitions]

    assert "priority" in keys
    assert "dealstage" not in keys
```

- [ ] **Step 2: Run the regression tests to verify the source split stays intact**

Run:
```bash
uv run pytest tests/api/routers/crm/test_deals_handler.py tests/service/crm/internal/test_custom_field_source_filtering.py tests/service/integrations/crm/common/test_field_registry_service.py -q
```

Expected:
- PASS

- [ ] **Step 3: Commit**

```bash
git add tests/api/routers/crm/test_deals_handler.py tests/service/crm/internal/test_custom_field_source_filtering.py tests/service/integrations/crm/common/test_field_registry_service.py
git commit -m "test: lock internal and provider field registry separation"
```

---

## Task 7: Update Documentation

**Files:**
- Modify: `docs/Technical Documentation/integrations/architecture-overview.md`
- Modify: `docs/Technical Documentation/integrations/sync-by-provider.md`
- Modify: `docs/API Documentation/deals/crm-custom-fields-api.md`

- [ ] **Step 1: Update the architecture docs to show the CRM schema-sync subtree**

```md
src/
  service/integrations/crm/
    common/
      schema_types.py
      field_type_mapper.py
      field_registry_service.py
    schema/
      hubspot_deal_schema_service.py

  workers/integrations_worker/providers/crm/hubspot/
    deal_schema_fetcher.py
```

- [ ] **Step 2: Update `sync-by-provider.md` to document the first active CRM sync slice**

```md
## HubSpot CRM

- **Custom OAuth**: Supported
- **Deal schema sync**: Supported
- **Deal record sync**: Deferred
- **Current behavior**: HubSpot deal property definitions are mirrored into
  `crm_custom_field_definitions` as `field_source=provider` rows keyed by integration.
```

- [ ] **Step 3: Update the custom-fields API doc to reflect the shared registry table**

```md
### Internal-only scope

This API is for **internally-managed** custom columns. Provider-backed field
definitions from HubSpot/Salesforce may live in the same registry table
(`crm_custom_field_definitions`), but all API reads and writes in this surface
are filtered to `field_source=internal`.
```

- [ ] **Step 4: Run a grep-based verification of the new terminology**

Run:
```bash
rg -n "field_source=internal|deal schema sync|shared registry" "docs/Technical Documentation/integrations" "docs/API Documentation/deals/crm-custom-fields-api.md"
```

Expected:
- matches in all three updated docs

- [ ] **Step 5: Commit**

```bash
git add "docs/Technical Documentation/integrations/architecture-overview.md" "docs/Technical Documentation/integrations/sync-by-provider.md" "docs/API Documentation/deals/crm-custom-fields-api.md"
git commit -m "docs: document hubspot deal schema sync architecture"
```

---

## Final Verification

**Files:**
- Verify all files touched in Tasks 1-7

- [ ] **Step 1: Run the focused test suite**

Run:
```bash
uv run pytest tests/service/crm/internal/test_custom_field_source_filtering.py tests/api/routers/crm/test_custom_fields_handler.py tests/api/routers/crm/test_deals_handler.py tests/service/integrations/crm/common/test_field_type_mapper.py tests/service/integrations/crm/common/test_field_registry_service.py tests/service/integrations/crm/schema/test_hubspot_deal_schema_service.py tests/workers/integrations_worker/providers/crm/hubspot/test_deal_schema_fetcher.py -q
```

Expected:
- all tests pass

- [ ] **Step 2: Run Ruff on the touched implementation and test files**

Run:
```bash
uv run ruff check src/service/crm/internal/custom_field_service.py src/service/crm/internal/custom_field_write_service.py src/service/crm/internal/deal_write_service.py src/api/routers/crm/deals_handler.py src/api/routers/crm/deal_import_handler.py src/service/integrations/crm src/workers/integrations_worker/providers/crm/hubspot tests/service/crm/internal/test_custom_field_source_filtering.py tests/api/routers/crm/test_custom_fields_handler.py tests/service/integrations/crm tests/workers/integrations_worker/providers/crm/hubspot
```

Expected:
- `All checks passed!`

- [ ] **Step 3: Run a focused import/compile sanity check**

Run:
```bash
uv run python -m compileall src/service/integrations/crm src/workers/integrations_worker/providers/crm/hubspot
```

Expected:
- compile succeeds with no syntax errors

- [ ] **Step 4: Commit the verification-safe final state**

```bash
git add -A
git commit -m "feat: add hubspot deal schema sync foundation"
```

---

## Self-Review

- **Spec coverage:** The plan covers the spec’s missing sequencing requirement first by isolating internal reads before provider rows are written, then implements the reusable CRM schema common layer, HubSpot fetcher, HubSpot orchestration service, and documentation updates.
- **Placeholder scan:** No `TODO`/`TBD` placeholders remain; each task includes real file paths, code snippets, and concrete verification commands.
- **Type consistency:** The plan consistently uses `CRMCustomFieldDefinition`, `CRMCustomFieldSource`, `NormalizedCRMFieldDefinition`, `FieldRegistryService`, `SchemaSyncResult`, and `HubSpotDealSchemaService` across tasks.
