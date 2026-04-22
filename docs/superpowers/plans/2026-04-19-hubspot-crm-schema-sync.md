# HubSpot CRM Schema Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a live HubSpot deal-schema sync that mirrors only non-canonical HubSpot properties into `crm_custom_field_definitions`, dispatches after connect, and refreshes every 14 days via a Kubernetes CronJob. No handlers/services expose the data yet; record sync and other objects come in later phases.

**Architecture:** Four pure units behind one orchestrator service, wrapped by a Tier 3 Celery task and a Tier 1 fan-out task. The Tier 3 task owns sync-history and connection-metadata sessions. The orchestrator owns the registry-write session. HubSpot API calls happen with no DB session held (fresh-session pattern, matches existing calendar/email workers).

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy async, Celery, httpx, existing `integrations_worker` infra (`call_with_token_retry`, `SyncHistoryManager`, `cron_dispatch.py` K8s-triggered scheduler, `send_integrations_task`).

**Reference spec:** `docs/superpowers/specs/2026-04-19-hubspot-crm-schema-sync-design.md`.

**Test policy:** Per spec Section 13, all tests are **one single end-phase task (Task 12)**. No per-step TDD. Tasks 1–11 are implementation-only; Task 12 adds every unit/component/integration/worker/regression test at once.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `src/service/integrations/crm/schema/__init__.py` | Create | Package marker for schema-sync module. |
| `src/service/integrations/crm/schema/hubspot_deal_mapping.py` | Create | HubSpot deal-property → `CRMDeal` column map + import-time self-validator. |
| `src/service/integrations/crm/schema/schema_types.py` | Create | `NormalizedSchemaOption`, `NormalizedSchemaField`, `CanonicalMatch`, `ProviderCustomMatch`, `Classification`, `SchemaSyncResult`, `RegistryWriteResult`. |
| `src/service/integrations/crm/schema/classifier.py` | Create | Pure `classify(raw)` function. |
| `src/service/integrations/crm/schema/normalizer.py` | Create | Pure `normalize(raw, classification)` function + HubSpot→internal type table. |
| `src/workers/integrations_worker/providers/crm/__init__.py` | Create (if missing) | CRM provider package marker. |
| `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` | Create (if missing) | HubSpot CRM provider package marker. |
| `src/workers/integrations_worker/providers/crm/hubspot/schema_fetcher.py` | Create | HubSpot Properties API fetcher with `call_with_token_retry`. |
| `src/service/integrations/crm/schema/registry_writer.py` | Create | `HubSpotDealRegistryWriter` — upsert/deactivate/reactivate under strict `field_source='provider'` scope. |
| `src/service/integrations/crm/schema/schema_sync_service.py` | Create | `HubSpotDealSchemaSyncService.run()` orchestrator with fresh-session pattern. |
| `src/workers/integrations_worker/tasks/crm/__init__.py` | Create | CRM task package exports. |
| `src/workers/integrations_worker/tasks/crm/hubspot_deal_schema_task.py` | Create | Tier 3 `integrations.sync_hubspot_deal_schema` Celery task. |
| `src/workers/integrations_worker/tasks/crm/hubspot_deal_schema_all_orgs_task.py` | Create | Tier 1 `integrations.sync_hubspot_deal_schema_all_orgs` fan-out task. |
| `src/workers/integrations_worker/tasks/__init__.py` | Modify | Export new CRM task modules. |
| `src/workers/integrations_worker/celery_app.py` | Modify | Import CRM task modules so Celery registers them. |
| `src/workers/integrations_worker/config.py` | Modify | Add task limits + routes for both new tasks. |
| `src/workers/integrations_worker/scheduled_jobs.py` | Modify | Register `sync-hubspot-deal-schema-all-orgs` schedule metadata. |
| `src/service/integrations/initial_sync_service.py` | Modify | Replace HubSpot placeholder-only metadata with queued-schema-sync metadata + Tier 3 dispatch; leave Salesforce as-is. |
| `src/workers/integrations_worker/README.md` | Modify | Document the new Tier 3 + Tier 1 CRM schema tasks and fresh-session flow. |
| `docs/Technical Documentation/integrations/architecture-overview.md` | Modify | Replace stale claims; state HubSpot deal schema sync is now live. |
| `docs/Technical Documentation/integrations/sync-by-provider.md` | Modify | Document HubSpot CRM deal schema sync as the first live CRM slice. |
| `tests/service/integrations/crm/schema/test_hubspot_deal_mapping.py` | Create (Task 11) | Mapping + self-validator unit tests. |
| `tests/service/integrations/crm/schema/test_classifier.py` | Create (Task 11) | `classify()` unit tests. |
| `tests/service/integrations/crm/schema/test_normalizer.py` | Create (Task 11) | `normalize()` + type-table unit tests. |
| `tests/workers/integrations_worker/providers/crm/hubspot/test_schema_fetcher.py` | Create (Task 11) | Fetcher tests (happy path, 401 refresh, 429/5xx retry, archived filter, malformed). |
| `tests/service/integrations/crm/schema/test_registry_writer.py` | Create (Task 11) | Upsert/deactivate/reactivate + source isolation. |
| `tests/service/integrations/crm/schema/test_schema_sync_service.py` | Create (Task 11) | End-to-end service run with fixture payload. |
| `tests/workers/integrations_worker/tasks/crm/test_hubspot_deal_schema_task.py` | Create (Task 11) | Tier 3 task success/config-fail/timeout + sync-history calls. |
| `tests/workers/integrations_worker/tasks/crm/test_hubspot_deal_schema_all_orgs_task.py` | Create (Task 11) | Tier 1 task dispatches per active HubSpot integration; skips inactive. |
| `tests/service/integrations/test_hubspot_initial_sync_dispatch.py` | Create (Task 11) | Post-connect: HubSpot dispatches Tier 3 + queued metadata; Salesforce unchanged. |
| `tests/service/integrations/test_hubspot_oauth_flow.py` | Modify (Task 11) | Update metadata expectations to reflect queued schema sync. |

---

> **Deferred:** the earlier draft of this plan opened with a task to add `field_source='internal'` filters to `custom_field_service`, `custom_field_write_service`, `deal_write_service`, and `deals_handler`. That work is **out of scope** for this phase. Provider rows written by this sync are not surfaced to any user-visible flow yet, and the read-path redesign lands with the consumption-layer spec. Do not modify any file under `src/service/crm/internal/` or `src/api/routers/crm/` in this plan.

## Task 1: Pre-defined HubSpot deal mapping file

**Files:**
- Create: `src/service/integrations/crm/schema/__init__.py`
- Create: `src/service/integrations/crm/schema/hubspot_deal_mapping.py`

- [ ] **Step 1: Create the schema package marker**

Create `src/service/integrations/crm/schema/__init__.py` as an empty file.

- [ ] **Step 2: Create the mapping module**

Create `src/service/integrations/crm/schema/hubspot_deal_mapping.py` with exactly this content:

```python
"""HubSpot deal-property → internal CRMDeal column map.

Single source of truth for which HubSpot deal properties are already canonical
on our side. Any HubSpot property present in this map is excluded from
crm_custom_field_definitions during schema sync.

Editing rule: add/remove entries here when CRMDeal gains or loses a canonical
column, or when we confirm a HubSpot property name whose semantics match an
existing CRMDeal column. Do not classify by label similarity; keys are exact
HubSpot property names as returned by the Properties API.

This file is also the field-level data map that the future HubSpot record-sync
phase will use to populate CRMDeal columns from HubSpot payloads.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal

_RAW_HUBSPOT_DEAL_CANONICAL_FIELDS: dict[str, str] = {
    "dealname": "name",
    "amount": "amount",
    "deal_currency_code": "currency",
    "amount_in_home_currency": "amount_home_currency",
    "closedate": "close_date",
    "dealstage": "stage_external_id",
    "pipeline": "pipeline_external_id",
    "hubspot_owner_id": "owner_external_id",
    "dealtype": "deal_type",
    "description": "description",
    "hs_forecast_category": "forecast_category",
    "hs_priority": "priority",
    "hs_deal_stage_probability": "probability",
    "hs_projected_amount": "predicted_amount",
    "createdate": "source_created_at",
    "hs_lastmodifieddate": "source_modified_at",
    "hs_date_entered_current_stage": "stage_entered_at",
    "notes_last_updated": "last_activity_at",
    "days_to_close": "days_to_close",
    "closed_won_date": "closed_won_at",
    "closed_lost_reason": "closed_reason",
}


def _validate_mapping(raw: Mapping[str, str]) -> None:
    seen_keys: set[str] = set()
    seen_values: set[str] = set()
    for hubspot_property, canonical_column in raw.items():
        if hubspot_property != hubspot_property.lower() or not hubspot_property:
            raise ValueError(
                f"HubSpot property key must be non-empty lowercase: {hubspot_property!r}"
            )
        if hubspot_property in seen_keys:
            raise ValueError(f"Duplicate HubSpot property key: {hubspot_property!r}")
        seen_keys.add(hubspot_property)

        if canonical_column in seen_values:
            raise ValueError(
                f"Duplicate canonical column {canonical_column!r} "
                f"(claimed by {hubspot_property!r} and a previous entry)"
            )
        seen_values.add(canonical_column)

        if not hasattr(CRMDeal, canonical_column):
            raise ValueError(
                f"Canonical column {canonical_column!r} does not exist on CRMDeal "
                f"(mapped from HubSpot property {hubspot_property!r})"
            )


_validate_mapping(_RAW_HUBSPOT_DEAL_CANONICAL_FIELDS)

HUBSPOT_DEAL_CANONICAL_FIELDS: Mapping[str, str] = MappingProxyType(
    _RAW_HUBSPOT_DEAL_CANONICAL_FIELDS
)


def is_hubspot_deal_canonical(property_name: str) -> bool:
    """True if the HubSpot property name already maps to a canonical CRMDeal column."""
    return property_name in HUBSPOT_DEAL_CANONICAL_FIELDS


def canonical_column_for(property_name: str) -> str | None:
    """Return the CRMDeal attribute name for a HubSpot property, or None if non-canonical."""
    return HUBSPOT_DEAL_CANONICAL_FIELDS.get(property_name)
```

- [ ] **Step 3: Smoke-check the module loads**

Run:

```bash
uv run python -c "from src.service.integrations.crm.schema.hubspot_deal_mapping import HUBSPOT_DEAL_CANONICAL_FIELDS; print(len(HUBSPOT_DEAL_CANONICAL_FIELDS))"
```

Expected: `21`

- [ ] **Step 4: Commit Task 1**

```bash
git add src/service/integrations/crm/schema/__init__.py src/service/integrations/crm/schema/hubspot_deal_mapping.py
git commit -m "feat(crm-schema): add hubspot deal canonical mapping with self-validator"
```

---

## Task 2: Schema types (dataclasses)

**Files:**
- Create: `src/service/integrations/crm/schema/schema_types.py`

- [ ] **Step 1: Create the schema-types module**

Create `src/service/integrations/crm/schema/schema_types.py` with:

```python
"""Provider-neutral shapes used by HubSpot (and future CRM) schema sync.

Kept as plain dataclasses so classification and normalization can be pure
functions — no ORM, no session, no I/O at this layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Union
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
)


@dataclass(slots=True, frozen=True)
class CanonicalMatch:
    hubspot_property: str
    canonical_column: str


@dataclass(slots=True, frozen=True)
class ProviderCustomMatch:
    hubspot_property: str
    is_provider_custom: bool


Classification = Union[CanonicalMatch, ProviderCustomMatch]


@dataclass(slots=True)
class NormalizedSchemaOption:
    value: str
    label: str
    description: str | None = None
    display_order: int | None = None
    hidden: bool = False


@dataclass(slots=True)
class NormalizedSchemaField:
    provider_field_key: str
    key: str
    label: str
    description: str | None
    data_type: CRMCustomFieldDataType
    is_provider_custom: bool
    display_order: int | None = None
    options: list[NormalizedSchemaOption] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    formula: dict[str, Any] | None = None
    data_payload: dict[str, Any] = field(default_factory=dict)
    source_modified_at: datetime | None = None


@dataclass(slots=True)
class RegistryWriteResult:
    created: int = 0
    updated: int = 0
    reactivated: int = 0
    deactivated: int = 0
    rows: list[CRMCustomFieldDefinition] = field(default_factory=list)


@dataclass(slots=True)
class SchemaSyncResult:
    provider: str
    object_type: str
    organisation_id: UUID
    integration_id: UUID
    fetched_properties: int
    canonical_properties: int
    custom_properties_persisted: int
    created: int
    updated: int
    reactivated: int
    deactivated: int
    skipped: int
    started_at: datetime
    completed_at: datetime

    def to_metadata(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "object_type": self.object_type,
            "fetched_properties": self.fetched_properties,
            "canonical_properties": self.canonical_properties,
            "custom_properties_persisted": self.custom_properties_persisted,
            "created": self.created,
            "updated": self.updated,
            "reactivated": self.reactivated,
            "deactivated": self.deactivated,
            "skipped": self.skipped,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
        }
```

- [ ] **Step 2: Smoke-check the module loads**

Run:

```bash
uv run python -c "from src.service.integrations.crm.schema.schema_types import NormalizedSchemaField, SchemaSyncResult; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit Task 2**

```bash
git add src/service/integrations/crm/schema/schema_types.py
git commit -m "feat(crm-schema): add provider-neutral schema dataclasses"
```

---

## Task 3: Classifier (pure function)

**Files:**
- Create: `src/service/integrations/crm/schema/classifier.py`

- [ ] **Step 1: Create the classifier module**

Create `src/service/integrations/crm/schema/classifier.py`:

```python
"""Pure classification of raw HubSpot deal properties.

Given a raw HubSpot property dict, decide whether it maps to an existing
canonical CRMDeal column or becomes a provider-backed custom-field row.
No I/O, no session, no normalization.
"""

from __future__ import annotations

from typing import Any, Mapping

from src.service.integrations.crm.schema.hubspot_deal_mapping import (
    canonical_column_for,
)
from src.service.integrations.crm.schema.schema_types import (
    CanonicalMatch,
    Classification,
    ProviderCustomMatch,
)


class MalformedHubSpotProperty(ValueError):
    """Raised when a raw HubSpot property is missing the fields we require to classify."""


def classify(raw: Mapping[str, Any]) -> Classification:
    name_value = raw.get("name")
    if not isinstance(name_value, str) or not name_value:
        raise MalformedHubSpotProperty(
            f"HubSpot property missing non-empty string 'name': {raw!r}"
        )

    column = canonical_column_for(name_value)
    if column is not None:
        return CanonicalMatch(hubspot_property=name_value, canonical_column=column)

    return ProviderCustomMatch(
        hubspot_property=name_value,
        is_provider_custom=not bool(raw.get("hubspotDefined", False)),
    )
```

- [ ] **Step 2: Smoke-check the module loads**

Run:

```bash
uv run python -c "from src.service.integrations.crm.schema.classifier import classify; print(classify({'name': 'dealname'}))"
```

Expected: a `CanonicalMatch(hubspot_property='dealname', canonical_column='name')` repr.

- [ ] **Step 3: Commit Task 3**

```bash
git add src/service/integrations/crm/schema/classifier.py
git commit -m "feat(crm-schema): add hubspot deal property classifier"
```

---

## Task 4: Normalizer (pure function + type table)

**Files:**
- Create: `src/service/integrations/crm/schema/normalizer.py`

- [ ] **Step 1: Create the normalizer module**

Create `src/service/integrations/crm/schema/normalizer.py`:

```python
"""Pure normalization of raw HubSpot deal properties into NormalizedSchemaField.

Only invoked for ProviderCustomMatch classifications — canonical matches are
discarded before this step. Produces exactly the shape the registry writer
persists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping

from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.schema.schema_types import (
    NormalizedSchemaField,
    NormalizedSchemaOption,
    ProviderCustomMatch,
)

logger = get_logger(__name__)


def map_hubspot_field_type(raw: Mapping[str, Any]) -> CRMCustomFieldDataType:
    raw_type = str(raw.get("type") or "").lower()
    field_type = str(raw.get("fieldType") or "").lower()
    display_hint = str(raw.get("numberDisplayHint") or "").lower()
    calculated = bool(raw.get("calculated"))
    formula = raw.get("calculationFormula")

    if calculated and isinstance(formula, str) and formula.strip():
        return CRMCustomFieldDataType.FORMULA

    if raw_type == "enumeration" and field_type in {"checkbox", "multiple_checkboxes"}:
        return CRMCustomFieldDataType.MULTISELECT
    if raw_type == "enumeration":
        return CRMCustomFieldDataType.SELECT

    if raw_type == "number" and display_hint == "currency":
        return CRMCustomFieldDataType.CURRENCY
    if raw_type == "number" and display_hint == "percentage":
        return CRMCustomFieldDataType.PERCENTAGE
    if raw_type == "number":
        return CRMCustomFieldDataType.NUMBER

    if raw_type == "bool":
        return CRMCustomFieldDataType.BOOLEAN
    if raw_type == "date":
        return CRMCustomFieldDataType.DATE
    if raw_type == "datetime":
        return CRMCustomFieldDataType.DATETIME
    if raw_type == "string":
        return CRMCustomFieldDataType.TEXT

    logger.warning(
        "Unrecognized HubSpot type, falling back to text",
        extra={
            "property": raw.get("name"),
            "type": raw.get("type"),
            "fieldType": raw.get("fieldType"),
        },
    )
    return CRMCustomFieldDataType.TEXT


def _parse_hubspot_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_options(raw_options: Any) -> list[NormalizedSchemaOption]:
    if not isinstance(raw_options, list):
        return []
    result: list[NormalizedSchemaOption] = []
    for opt in raw_options:
        if not isinstance(opt, Mapping):
            continue
        value = str(opt.get("value") or "")
        if not value:
            continue
        label_raw = opt.get("label")
        label = str(label_raw) if label_raw else value
        description = opt.get("description") or None
        display_order = opt.get("displayOrder")
        if display_order is not None and not isinstance(display_order, int):
            display_order = None
        result.append(
            NormalizedSchemaOption(
                value=value,
                label=label,
                description=description if isinstance(description, str) else None,
                display_order=display_order,
                hidden=bool(opt.get("hidden", False)),
            )
        )
    return result


def normalize(
    raw: Mapping[str, Any],
    classification: ProviderCustomMatch,
) -> NormalizedSchemaField:
    name = classification.hubspot_property
    data_type = map_hubspot_field_type(raw)

    formula: dict[str, Any] | None = None
    if data_type == CRMCustomFieldDataType.FORMULA:
        formula = {"source": "hubspot", "expression": raw.get("calculationFormula")}

    label_raw = raw.get("label")
    label = str(label_raw) if isinstance(label_raw, str) and label_raw else name

    description_raw = raw.get("description")
    description = description_raw if isinstance(description_raw, str) and description_raw else None

    display_order = raw.get("displayOrder")
    if display_order is not None and not isinstance(display_order, int):
        display_order = None

    return NormalizedSchemaField(
        provider_field_key=name,
        key=name,
        label=label,
        description=description,
        data_type=data_type,
        is_provider_custom=classification.is_provider_custom,
        display_order=display_order,
        options=_normalize_options(raw.get("options")),
        validation={},
        formula=formula,
        data_payload=dict(raw),
        source_modified_at=_parse_hubspot_timestamp(raw.get("updatedAt"))
        or _parse_hubspot_timestamp(raw.get("createdAt")),
    )


def options_payload(options: list[NormalizedSchemaOption]) -> dict[str, Any]:
    """Shape the options list into the JSONB column format: {values: [...], source: 'hubspot'}."""
    return {
        "source": "hubspot",
        "values": [
            {
                "value": opt.value,
                "label": opt.label,
                "description": opt.description,
                "display_order": opt.display_order,
                "hidden": opt.hidden,
            }
            for opt in options
        ],
    }


# Re-export: kept here so we don't re-import UTC in callers that format timestamps.
__all__ = ["map_hubspot_field_type", "normalize", "options_payload", "UTC"]
```

- [ ] **Step 2: Smoke-check the module loads**

Run:

```bash
uv run python -c "from src.service.integrations.crm.schema.normalizer import normalize, map_hubspot_field_type; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit Task 4**

```bash
git add src/service/integrations/crm/schema/normalizer.py
git commit -m "feat(crm-schema): add hubspot property normalizer and type mapper"
```

---

## Task 5: HubSpot schema fetcher

**Files:**
- Create (if missing): `src/workers/integrations_worker/providers/crm/__init__.py`
- Create (if missing): `src/workers/integrations_worker/providers/crm/hubspot/__init__.py`
- Create: `src/workers/integrations_worker/providers/crm/hubspot/schema_fetcher.py`

- [ ] **Step 1: Ensure package markers exist**

Check whether `src/workers/integrations_worker/providers/crm/__init__.py` and `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` already exist. If either is missing, create it as an empty file.

- [ ] **Step 2: Create the fetcher module**

Create `src/workers/integrations_worker/providers/crm/hubspot/schema_fetcher.py`:

```python
"""HubSpot Properties API fetcher.

Fetches raw property definitions for a CRM object type (deals today; contacts
and companies later). No classification, no DB — just HTTP with token refresh
and archived-filtering.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)

logger = get_logger(__name__)

_BASE_URL = "https://api.hubapi.com/crm/v3/properties"
_DEFAULT_TIMEOUT = 30.0


class HubSpotSchemaFetcher:
    def __init__(self, *, token_info: IntegrationTokenInfo) -> None:
        self.token_info = token_info

    async def fetch_properties(self, *, object_type: str) -> list[dict[str, Any]]:
        url = f"{_BASE_URL}/{object_type}"

        async def _request(access_token: str) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"archived": "false"},
                )
                response.raise_for_status()
                return response

        response = await call_with_token_retry(self.token_info, _request)
        payload = response.json()
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError(
                f"HubSpot {object_type} properties API returned non-list results"
            )
        logger.info(
            "Fetched HubSpot schema",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "object_type": object_type,
                "count": len(results),
            },
        )
        return results
```

- [ ] **Step 3: Smoke-check the module loads**

Run:

```bash
uv run python -c "from src.workers.integrations_worker.providers.crm.hubspot.schema_fetcher import HubSpotSchemaFetcher; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit Task 5**

```bash
git add src/workers/integrations_worker/providers/crm/__init__.py src/workers/integrations_worker/providers/crm/hubspot/__init__.py src/workers/integrations_worker/providers/crm/hubspot/schema_fetcher.py
git commit -m "feat(crm-schema): add hubspot properties api fetcher"
```

(If a package `__init__` already existed, drop it from the `git add` line.)

---

## Task 6: Registry writer

**Files:**
- Create: `src/service/integrations/crm/schema/registry_writer.py`

- [ ] **Step 1: Create the registry writer module**

Create `src/service/integrations/crm/schema/registry_writer.py`:

```python
"""Provider-backed persistence for HubSpot deal schema sync.

Owns all writes to crm_custom_field_definitions where field_source='provider'
for the deal entity scope. Strictly refuses to touch field_source='internal'
rows. Upserts active rows and deactivates previously-seen rows that are missing
from the current sync payload (active-snapshot semantics).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.schema.normalizer import options_payload
from src.service.integrations.crm.schema.schema_types import (
    NormalizedSchemaField,
    RegistryWriteResult,
)

logger = get_logger(__name__)


class HubSpotDealRegistryWriter:
    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id

    async def apply(
        self,
        *,
        fields: list[NormalizedSchemaField],
    ) -> RegistryWriteResult:
        existing = await self._load_existing()
        now = datetime.now(UTC)

        result = RegistryWriteResult()
        incoming_keys: set[str] = set()

        for nf in fields:
            incoming_keys.add(nf.provider_field_key)
            row = existing.get(nf.provider_field_key)
            if row is None:
                new_row = self._build_row(nf, now=now)
                self.session.add(new_row)
                result.rows.append(new_row)
                result.created += 1
                continue

            was_active = bool(row.is_active)
            self._apply_fields(row, nf, now=now)
            row.is_active = True
            result.rows.append(row)
            if was_active:
                result.updated += 1
            else:
                result.reactivated += 1

        for key, row in existing.items():
            if key in incoming_keys:
                continue
            if row.is_active:
                row.is_active = False
                result.deactivated += 1

        await self.session.flush()
        logger.info(
            "HubSpot deal registry write applied",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "created": result.created,
                "updated": result.updated,
                "reactivated": result.reactivated,
                "deactivated": result.deactivated,
            },
        )
        return result

    async def _load_existing(self) -> dict[str, CRMCustomFieldDefinition]:
        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.integration_id) == self.integration_id,
            col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.PROVIDER.value,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        rows = result.scalars().all()
        out: dict[str, CRMCustomFieldDefinition] = {}
        for row in rows:
            if row.provider_field_key is None:
                continue
            out[row.provider_field_key] = row
        return out

    def _build_row(
        self,
        nf: NormalizedSchemaField,
        *,
        now: datetime,
    ) -> CRMCustomFieldDefinition:
        return CRMCustomFieldDefinition(
            organisation_id=self.organisation_id,
            entity_type=CRMCustomFieldEntity.DEAL,
            field_source=CRMCustomFieldSource.PROVIDER,
            integration_id=self.integration_id,
            provider_name="hubspot",
            key=nf.key,
            provider_field_key=nf.provider_field_key,
            label=nf.label,
            description=nf.description,
            data_type=nf.data_type,
            required=False,
            is_predefined=False,
            is_provider_custom=nf.is_provider_custom,
            display_order=nf.display_order,
            options=options_payload(nf.options),
            validation=nf.validation or {},
            formula=nf.formula,
            data_payload=nf.data_payload or None,
            source_modified_at=nf.source_modified_at,
            last_seen_at=now,
            is_active=True,
            is_deleted=False,
        )

    def _apply_fields(
        self,
        row: CRMCustomFieldDefinition,
        nf: NormalizedSchemaField,
        *,
        now: datetime,
    ) -> None:
        row.key = nf.key
        row.label = nf.label
        row.description = nf.description
        row.data_type = nf.data_type
        row.is_provider_custom = nf.is_provider_custom
        row.display_order = nf.display_order
        row.options = options_payload(nf.options)
        row.validation = nf.validation or {}
        row.formula = nf.formula
        row.data_payload = nf.data_payload or None
        row.source_modified_at = nf.source_modified_at
        row.last_seen_at = now
```

- [ ] **Step 2: Smoke-check the module loads**

Run:

```bash
uv run python -c "from src.service.integrations.crm.schema.registry_writer import HubSpotDealRegistryWriter; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit Task 6**

```bash
git add src/service/integrations/crm/schema/registry_writer.py
git commit -m "feat(crm-schema): add hubspot deal registry writer with active-snapshot semantics"
```

---

## Task 7: Schema sync service orchestrator

**Files:**
- Create: `src/service/integrations/crm/schema/schema_sync_service.py`

- [ ] **Step 1: Create the service module**

Create `src/service/integrations/crm/schema/schema_sync_service.py`:

```python
"""HubSpot deal schema sync orchestrator.

Fresh-session pattern:
  Session A  — load integration, extract IntegrationTokenInfo, release.
  (no session)  — HubSpot Properties API fetch with token retry.
  (no session)  — pure-Python classify + normalize pass.
  Session B  — registry writer upsert/deactivate, commit, release.

The Tier 3 Celery task that wraps this service owns sync-history and
connection-metadata sessions (see tasks/crm/hubspot_deal_schema_task.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.schema.classifier import (
    MalformedHubSpotProperty,
    classify,
)
from src.service.integrations.crm.schema.normalizer import normalize
from src.service.integrations.crm.schema.registry_writer import (
    HubSpotDealRegistryWriter,
)
from src.service.integrations.crm.schema.schema_types import (
    CanonicalMatch,
    NormalizedSchemaField,
    ProviderCustomMatch,
    RegistryWriteResult,
    SchemaSyncResult,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.hubspot.schema_fetcher import (
    HubSpotSchemaFetcher,
)

logger = get_logger(__name__)


class SchemaSyncConfigurationError(ValueError):
    """Raised when the integration cannot be used for schema sync (not HubSpot, inactive, missing)."""


class HubSpotDealSchemaSyncService:
    PROVIDER = "hubspot"
    OBJECT_TYPE = "deal"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SchemaSyncResult:
        started_at = datetime.now(UTC)
        token_info = await self._load_token_info(organisation_id, integration_id)

        raw_fields = await HubSpotSchemaFetcher(token_info=token_info).fetch_properties(
            object_type="deals"
        )

        canonical_count = 0
        skipped_count = 0
        normalized: list[NormalizedSchemaField] = []
        for raw in raw_fields:
            try:
                classification = classify(raw)
            except MalformedHubSpotProperty:
                skipped_count += 1
                logger.warning(
                    "Skipping malformed HubSpot property",
                    extra={
                        "integration_id": str(integration_id),
                        "raw_keys": list(raw.keys()) if isinstance(raw, Mapping) else None,
                    },
                )
                continue

            if isinstance(classification, CanonicalMatch):
                canonical_count += 1
                continue

            assert isinstance(classification, ProviderCustomMatch)
            normalized.append(normalize(raw, classification))

        write_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            fields=normalized,
        )

        completed_at = datetime.now(UTC)
        return SchemaSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            fetched_properties=len(raw_fields),
            canonical_properties=canonical_count,
            custom_properties_persisted=len(normalized),
            created=write_result.created,
            updated=write_result.updated,
            reactivated=write_result.reactivated,
            deactivated=write_result.deactivated,
            skipped=skipped_count,
            started_at=started_at,
            completed_at=completed_at,
        )

    async def _load_token_info(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> IntegrationTokenInfo:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise SchemaSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.HUBSPOT:
                raise SchemaSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise SchemaSyncConfigurationError(
                    f"Integration {integration_id} is not active"
                )
            return IntegrationTokenInfo.from_integration(integration, organisation_id)
        raise SchemaSyncConfigurationError(
            "Could not acquire org session for schema sync"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        fields: list[NormalizedSchemaField],
    ) -> RegistryWriteResult:
        async for session in acquire_org_async_session(organisation_id):
            writer = HubSpotDealRegistryWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(fields=fields)
            await session.commit()
            return write_result
        raise SchemaSyncConfigurationError(
            "Could not acquire org session for schema registry write"
        )
```

- [ ] **Step 2: Smoke-check the module loads**

Run:

```bash
uv run python -c "from src.service.integrations.crm.schema.schema_sync_service import HubSpotDealSchemaSyncService; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit Task 7**

```bash
git add src/service/integrations/crm/schema/schema_sync_service.py
git commit -m "feat(crm-schema): add hubspot deal schema sync orchestrator"
```

---

## Task 8: Tier 3 Celery task

**Files:**
- Create: `src/workers/integrations_worker/tasks/crm/__init__.py`
- Create: `src/workers/integrations_worker/tasks/crm/hubspot_deal_schema_task.py`
- Modify: `src/workers/integrations_worker/config.py`
- Modify: `src/workers/integrations_worker/tasks/__init__.py`
- Modify: `src/workers/integrations_worker/celery_app.py`

- [ ] **Step 1: Add task limits + routes to `config.py`**

In `src/workers/integrations_worker/config.py`, inside the `TASK_LIMITS` dict, add under the `# ── Tier 3 ──` section:

```python
    "integrations.sync_hubspot_deal_schema": {
        "soft": 900,
        "hard": 960,
        "retries": 3,
        "retry_delay": 60,
    },
```

And **also** add a new Tier 1 entry (used by Task 9):

```python
    "integrations.sync_hubspot_deal_schema_all_orgs": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
```

In the `task_routes` dict inside `IntegrationsWorkerConfig.__init__`, replace the stale comment `# CRM sync tasks (HubSpot, Salesforce) deprecated — moved to deprecated/workers/` with:

```python
            # CRM schema sync tasks (HubSpot)
            "integrations.sync_hubspot_deal_schema_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_hubspot_deal_schema": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
```

- [ ] **Step 2: Create the CRM tasks package**

Create `src/workers/integrations_worker/tasks/crm/__init__.py`:

```python
"""CRM schema and record sync Celery tasks for integrations_worker."""

from src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task import (
    sync_hubspot_deal_schema_task,
)

__all__ = ["sync_hubspot_deal_schema_task"]
```

- [ ] **Step 3: Create the Tier 3 task module**

Create `src/workers/integrations_worker/tasks/crm/hubspot_deal_schema_task.py`:

```python
"""Tier 3: HubSpot deal schema sync for a single integration.

Triggered by:
  - InitialSyncService post-connect dispatch
  - Tier 1 fan-out task on the 14-day schedule
  - Future "Refresh schema" manual endpoint

Session ownership:
  - Task owns sync-history and connection-metadata sessions (Session 1: start,
    Session 3: complete + metadata).
  - HubSpotDealSchemaSyncService owns its own extract + persist sessions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.schema.schema_sync_service import (
    HubSpotDealSchemaSyncService,
    SchemaSyncConfigurationError,
)
from src.service.integrations.sync_health import record_sync_failure
from src.service.integrations.sync_history_manager import SyncHistoryManager
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_hubspot_deal_schema"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_hubspot_deal_schema_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        logger.info(
            "HubSpot deal schema sync starting (Tier 3)",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )

        sync_id: UUID | None = None
        async for session in acquire_org_async_session(org_id):
            sync_id = await SyncHistoryManager.start_fresh(session, int_id)

        try:
            result = await HubSpotDealSchemaSyncService().run(
                organisation_id=org_id,
                integration_id=int_id,
            )
        except SchemaSyncConfigurationError as exc:
            await _mark_history_failed(
                org_id,
                sync_id,
                error_class="configuration",
                error_message=str(exc),
            )
            raise

        async for session in acquire_org_async_session(org_id):
            await SyncHistoryManager.complete_fresh(
                session,
                sync_id,  # type: ignore[arg-type]
                status=SyncStatus.SUCCESS,
                records_processed=result.custom_properties_persisted,
                metadata={
                    "entity": "schema",
                    **result.to_metadata(),
                },
            )
            await _update_integration_metadata(
                session=session,
                integration_id=int_id,
                status="success",
                result_metadata=result.to_metadata(),
                error_class=None,
                error_message=None,
            )

        completed_at = datetime.now(UTC).isoformat()
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": completed_at,
            **result.to_metadata(),
        }

    try:
        return run_async(_run())
    except SoftTimeLimitExceeded:
        run_async(
            record_sync_failure(
                organisation_id=org_id,
                integration_id=int_id,
                error=TimeoutError("HubSpot deal schema sync timed out"),
            )
        )
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }


async def _mark_history_failed(
    org_id: UUID,
    sync_id: UUID | None,
    *,
    error_class: str,
    error_message: str,
) -> None:
    if sync_id is None:
        return
    async for session in acquire_org_async_session(org_id):
        await SyncHistoryManager.complete_fresh(
            session,
            sync_id,
            status=SyncStatus.FAILED,
            records_processed=0,
            metadata={"entity": "schema", "error_class": error_class},
            error_details={"message": error_message, "class": error_class},
        )


async def _update_integration_metadata(
    *,
    session,
    integration_id: UUID,
    status: str,
    result_metadata: dict[str, Any],
    error_class: str | None,
    error_message: str | None,
) -> None:
    stmt = select(KnowledgeIntegration).where(
        col(KnowledgeIntegration.integration_id) == integration_id,
    )
    integration = (await session.execute(stmt)).scalar_one_or_none()
    if integration is None:
        return
    metadata = dict(integration.connection_metadata or {})
    schema_sync = dict(metadata.get("schema_sync") or {})
    deals_entry = {
        "status": status,
        "last_synced_at": datetime.now(UTC).isoformat(),
        "fetched_properties": result_metadata.get("fetched_properties"),
        "canonical_properties": result_metadata.get("canonical_properties"),
        "custom_properties_persisted": result_metadata.get("custom_properties_persisted"),
        "created": result_metadata.get("created"),
        "updated": result_metadata.get("updated"),
        "reactivated": result_metadata.get("reactivated"),
        "deactivated": result_metadata.get("deactivated"),
        "skipped": result_metadata.get("skipped"),
        "error_class": error_class,
        "error_message": error_message,
    }
    schema_sync["deals"] = deals_entry
    metadata["schema_sync"] = schema_sync
    if status == "success":
        metadata["initial_sync_status"] = "success"
    integration.connection_metadata = metadata
    integration.last_synced_at = datetime.now(UTC)
    session.add(integration)
    await session.commit()
```

- [ ] **Step 4: Export the new task from the worker `tasks/__init__.py`**

In `src/workers/integrations_worker/tasks/__init__.py`:

1. Remove the stale docstring line `CRM sync tasks (HubSpot, Salesforce) deprecated — moved to deprecated/workers/.` — it no longer reflects reality.
2. Add a new import block below the existing `from .token_refresh import ...` block:

```python
# CRM schema sync tasks (HubSpot)
from .crm import (
    sync_hubspot_deal_schema_task,
)
```

3. Append to the existing `__all__` list at the bottom of the file:

```python
    # CRM schema sync (HubSpot)
    "sync_hubspot_deal_schema_task",
```

- [ ] **Step 5: Import the CRM task package from `celery_app.py` so Celery registers the task**

In `src/workers/integrations_worker/celery_app.py`:

1. Replace the stale line `# CRM sync (HubSpot, Salesforce) deprecated — moved to deprecated/workers/` with `# CRM schema sync tasks (HubSpot)`.
2. Add `crm` to the existing `from .tasks import (...)` block, matching the noqa-comment style of the other entries. After the edit the block reads:

```python
# Explicitly import task modules to ensure registration
# Three-tier architecture: calendar, email, messaging, and CRM schema sync tasks
from .tasks import (
    calendar,  # noqa: F401, E402 - three-tier calendar sync
    crm,  # noqa: F401, E402 - three-tier CRM schema sync (HubSpot)
    email,  # noqa: F401, E402 - three-tier email sync
    messaging,  # noqa: F401, E402 - three-tier messaging sync
    token_refresh,  # noqa: F401, E402
)
```

- [ ] **Step 6: Smoke-check Celery picks up the new task**

Run:

```bash
uv run python -c "from src.workers.integrations_worker.celery_app import app; print('integrations.sync_hubspot_deal_schema' in app.tasks)"
```

Expected: `True`

- [ ] **Step 7: Commit Task 8**

```bash
git add src/workers/integrations_worker/config.py src/workers/integrations_worker/tasks/__init__.py src/workers/integrations_worker/tasks/crm/__init__.py src/workers/integrations_worker/tasks/crm/hubspot_deal_schema_task.py src/workers/integrations_worker/celery_app.py
git commit -m "feat(crm-schema): add tier 3 hubspot deal schema celery task"
```

---

## Task 9: Tier 1 fan-out task + schedule entry

**Files:**
- Create: `src/workers/integrations_worker/tasks/crm/hubspot_deal_schema_all_orgs_task.py`
- Modify: `src/workers/integrations_worker/tasks/crm/__init__.py`
- Modify: `src/workers/integrations_worker/scheduled_jobs.py`

- [ ] **Step 1: Create the Tier 1 task module**

Create `src/workers/integrations_worker/tasks/crm/hubspot_deal_schema_all_orgs_task.py`:

```python
"""Tier 1: fan-out HubSpot deal schema sync for all active HubSpot integrations.

Triggered by the Kubernetes CronJob that calls
`python -m src.workers.integrations_worker.cron_dispatch --job sync-hubspot-deal-schema-all-orgs`.

Reads from the master schema only — per-org work is dispatched to the Tier 3
task. Never holds a per-org session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import send_integrations_task
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_hubspot_deal_schema_all_orgs"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_hubspot_deal_schema_all_orgs_task(self: Task) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()

    async def _run() -> dict[str, Any]:
        dispatched = 0
        skipped_inactive = 0

        async for session in acquire_master_async_session():
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_type) == IntegrationType.HUBSPOT,
                col(KnowledgeIntegration.is_active).is_(True),
            )
            rows = (await session.execute(stmt)).scalars().all()

        for row in rows:
            if not row.is_active:
                skipped_inactive += 1
                continue
            send_integrations_task(
                task_name="integrations.sync_hubspot_deal_schema",
                kwargs={
                    "organisation_id": str(row.organisation_id),
                    "integration_id": str(row.integration_id),
                },
            )
            dispatched += 1

        logger.info(
            "HubSpot deal schema fan-out complete",
            extra={"dispatched": dispatched, "skipped_inactive": skipped_inactive},
        )
        return {
            "status": "success",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "dispatched": dispatched,
            "skipped_inactive": skipped_inactive,
        }

    return run_async(_run())
```

- [ ] **Step 2: Export the new task from the CRM tasks package AND from the worker `tasks/__init__.py`**

First, update `src/workers/integrations_worker/tasks/crm/__init__.py` to:

```python
"""CRM schema and record sync Celery tasks for integrations_worker."""

from src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_all_orgs_task import (
    sync_hubspot_deal_schema_all_orgs_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task import (
    sync_hubspot_deal_schema_task,
)

__all__ = [
    "sync_hubspot_deal_schema_all_orgs_task",
    "sync_hubspot_deal_schema_task",
]
```

Then, update `src/workers/integrations_worker/tasks/__init__.py` to also export the new Tier 1 task. Change the CRM import block added in Task 8 Step 4 to:

```python
# CRM schema sync tasks (HubSpot)
from .crm import (
    sync_hubspot_deal_schema_all_orgs_task,
    sync_hubspot_deal_schema_task,
)
```

And append to `__all__`:

```python
    # CRM schema sync (HubSpot)
    "sync_hubspot_deal_schema_all_orgs_task",
    "sync_hubspot_deal_schema_task",
```

(Remove the earlier single-entry `"sync_hubspot_deal_schema_task"` line added in Task 8 Step 4 — it is replaced by this two-entry block.)

- [ ] **Step 3: Register the scheduled job metadata**

In `src/workers/integrations_worker/scheduled_jobs.py`, add a new entry to the `SCHEDULED_JOBS` dict:

```python
    "sync-hubspot-deal-schema-all-orgs": ScheduledJob(
        name="sync-hubspot-deal-schema-all-orgs",
        task_name="integrations.sync_hubspot_deal_schema_all_orgs",
        schedule="0 2 */14 * *",
        max_lag_seconds=60 * 60 * 48,
    ),
```

- [ ] **Step 4: Smoke-check Celery registers the new task and schedule**

Run:

```bash
uv run python -c "from src.workers.integrations_worker.celery_app import app; print('integrations.sync_hubspot_deal_schema_all_orgs' in app.tasks)"
uv run python -c "from src.workers.integrations_worker.scheduled_jobs import get_scheduled_job; print(get_scheduled_job('sync-hubspot-deal-schema-all-orgs').task_name)"
```

Expected:

```
True
integrations.sync_hubspot_deal_schema_all_orgs
```

- [ ] **Step 5: Commit Task 9**

```bash
git add src/workers/integrations_worker/tasks/crm/__init__.py src/workers/integrations_worker/tasks/crm/hubspot_deal_schema_all_orgs_task.py src/workers/integrations_worker/scheduled_jobs.py
git commit -m "feat(crm-schema): add tier 1 fan-out task and 14-day cronjob schedule"
```

---

## Task 10: Post-connect dispatch for HubSpot

**Files:**
- Modify: `src/service/integrations/initial_sync_service.py`

- [ ] **Step 1: Add a HubSpot branch to `apply_post_connect_state`**

In `src/service/integrations/initial_sync_service.py`, replace the body of `apply_post_connect_state` (currently lines 66–87) with:

```python
def apply_post_connect_state(self, integration: KnowledgeIntegration) -> None:
    """Apply provider-specific post-connect state before the integration is committed.

    HubSpot queues a schema sync; Salesforce retains the prior `not_implemented`
    placeholder until its own sync pipeline ships.
    """
    integration_type = integration.integration_type
    if isinstance(integration_type, str):
        integration_type = IntegrationType(integration_type)

    if integration_type not in self.CRM_PLACEHOLDER_INTEGRATION_TYPES:
        return

    metadata = dict(integration.connection_metadata or {})

    if integration_type == IntegrationType.HUBSPOT:
        metadata[self.INITIAL_SYNC_STATUS_METADATA_KEY] = "queued"
        metadata.pop(self.INITIAL_SYNC_PLACEHOLDER_METADATA_KEY, None)
        schema_sync = dict(metadata.get("schema_sync") or {})
        schema_sync["deals"] = {"status": "queued"}
        metadata["schema_sync"] = schema_sync
        integration.connection_metadata = metadata
        return

    # Salesforce (and any other CRM placeholder) keeps the existing behavior.
    metadata[self.INITIAL_SYNC_STATUS_METADATA_KEY] = "not_implemented"
    metadata[self.INITIAL_SYNC_PLACEHOLDER_METADATA_KEY] = self.CRM_PLACEHOLDER_MESSAGES[
        integration_type
    ]
    integration.connection_metadata = metadata
```

- [ ] **Step 2: Dispatch the Tier 3 task for HubSpot in `trigger_initial_sync`**

Still in `src/service/integrations/initial_sync_service.py`, replace the CRM placeholder branch (currently lines 128–134) with:

```python
            if integration_type == IntegrationType.HUBSPOT:
                self._trigger_hubspot_schema_sync(org_id_str, int_id_str)
                return

            if integration_type in self.CRM_PLACEHOLDER_INTEGRATION_TYPES:
                logger.info(
                    "Initial sync placeholder only for integration type %s (integration=%s)",
                    integration_type.value,
                    int_id_str,
                )
                return
```

Then add a new private method to the class, mirroring the style of `_trigger_calendar_sync`:

```python
    def _trigger_hubspot_schema_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Trigger HubSpot deal schema sync (Tier 3)."""
        send_integrations_task(
            task_name="integrations.sync_hubspot_deal_schema",
            kwargs={"organisation_id": org_id_str, "integration_id": int_id_str},
        )
        logger.info(
            "Triggered initial HubSpot deal schema sync: integration=%s",
            int_id_str,
        )
```

- [ ] **Step 3: Smoke-check import still works**

Run:

```bash
uv run python -c "from src.service.integrations.initial_sync_service import initial_sync_service; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit Task 10**

```bash
git add src/service/integrations/initial_sync_service.py
git commit -m "feat(crm-schema): dispatch hubspot schema sync on connect and queue metadata"
```

---

## Task 11: Documentation updates

**Files:**
- Modify: `src/workers/integrations_worker/README.md`
- Modify: `docs/Technical Documentation/integrations/architecture-overview.md`
- Modify: `docs/Technical Documentation/integrations/sync-by-provider.md`

- [ ] **Step 1: Document the new Tier 3 and Tier 1 tasks in the worker README**

Open `src/workers/integrations_worker/README.md`. Under the existing Tier 3 task list add:

```markdown
- **integrations.sync_hubspot_deal_schema** (Tier 3): fetches HubSpot deal
  property schema via the Properties API and mirrors non-canonical properties
  into `crm_custom_field_definitions` as `field_source='provider'` rows.
  Follows the fresh-session pattern: task owns sync-history and
  `connection_metadata.schema_sync.deals` updates; service owns extract
  (load integration + token info) and persist (registry upsert/deactivate)
  sessions.
```

Under the Tier 1 / schedule section add:

```markdown
- **integrations.sync_hubspot_deal_schema_all_orgs** (Tier 1): fan-out to
  every active HubSpot integration. Dispatched by the Kubernetes CronJob
  `sync-hubspot-deal-schema-all-orgs` every 14 days at 02:00 UTC via
  `cron_dispatch.py`. Holds no per-org session — reads from master only and
  dispatches per-integration Tier 3 tasks.
```

If the README has a "CRM" section marked as deprecated or unimplemented, replace those statements so they reflect that HubSpot **deal schema sync** is now live while record sync and other CRM objects remain deferred.

- [ ] **Step 2: Update `architecture-overview.md`**

Open `docs/Technical Documentation/integrations/architecture-overview.md`. Find the section that lists CRM status. Replace with:

```markdown
### CRM (HubSpot, Salesforce)

- **HubSpot custom OAuth**: live.
- **HubSpot deal schema sync**: live (`integrations.sync_hubspot_deal_schema`,
  dispatched after connect, refreshed every 14 days by a Kubernetes CronJob,
  reusable for future manual re-run).
- **HubSpot deal / contact / company record sync**: not yet implemented.
- **HubSpot schema sync for contacts, companies, owners, pipelines, stages**:
  not yet implemented.
- **Salesforce custom OAuth**: live.
- **Salesforce schema/record sync**: not yet implemented (post-connect applies
  a `not_implemented` placeholder).
```

- [ ] **Step 3: Update `sync-by-provider.md`**

Open `docs/Technical Documentation/integrations/sync-by-provider.md`. Add (or replace the existing HubSpot section with):

```markdown
## HubSpot CRM

- **Custom OAuth**: supported.
- **Deal schema sync**: supported.
  - Task: `integrations.sync_hubspot_deal_schema` (Tier 3).
  - Fan-out: `integrations.sync_hubspot_deal_schema_all_orgs` (Tier 1), scheduled
    every 14 days via the Kubernetes CronJob `sync-hubspot-deal-schema-all-orgs`.
  - Dispatched on post-connect by `InitialSyncService`.
  - Persistence rule: HubSpot properties with a matching canonical column on
    `CRMDeal` (see `src/service/integrations/crm/schema/hubspot_deal_mapping.py`)
    are **not** stored in `crm_custom_field_definitions`. Non-canonical HubSpot
    properties are persisted as `field_source='provider'` rows and further
    tagged with `is_provider_custom` (`true` = tenant custom inside HubSpot,
    `false` = HubSpot built-in we don't currently canonicalize).
  - Direction: HubSpot → internal only. No write-back.
- **Deal record sync**: deferred.
- **Contacts / companies / pipelines / stages / owners (schema and records)**: deferred.
```

- [ ] **Step 4: Commit Task 11**

```bash
git add 'src/workers/integrations_worker/README.md' 'docs/Technical Documentation/integrations/architecture-overview.md' 'docs/Technical Documentation/integrations/sync-by-provider.md'
git commit -m "docs(crm-schema): document hubspot deal schema sync as the first live crm slice"
```

---

## Task 12: End-phase test suite

Everything in Task 12 lands together. Do not split into per-module commits.

**Files:**
- Create: `tests/service/integrations/crm/schema/test_hubspot_deal_mapping.py`
- Create: `tests/service/integrations/crm/schema/test_classifier.py`
- Create: `tests/service/integrations/crm/schema/test_normalizer.py`
- Create: `tests/workers/integrations_worker/providers/crm/hubspot/test_schema_fetcher.py`
- Create: `tests/service/integrations/crm/schema/test_registry_writer.py`
- Create: `tests/service/integrations/crm/schema/test_schema_sync_service.py`
- Create: `tests/workers/integrations_worker/tasks/crm/test_hubspot_deal_schema_task.py`
- Create: `tests/workers/integrations_worker/tasks/crm/test_hubspot_deal_schema_all_orgs_task.py`
- Create: `tests/service/integrations/test_hubspot_initial_sync_dispatch.py`
- Modify: `tests/service/integrations/test_hubspot_oauth_flow.py` (update metadata expectations)

- [ ] **Step 1: Unit test the mapping self-validator**

Create `tests/service/integrations/crm/schema/test_hubspot_deal_mapping.py`:

```python
import pytest

from src.service.integrations.crm.schema.hubspot_deal_mapping import (
    HUBSPOT_DEAL_CANONICAL_FIELDS,
    canonical_column_for,
    is_hubspot_deal_canonical,
    _validate_mapping,
)


def test_dealname_maps_to_name() -> None:
    assert canonical_column_for("dealname") == "name"
    assert is_hubspot_deal_canonical("dealname") is True


def test_unknown_property_is_not_canonical() -> None:
    assert canonical_column_for("region__c") is None
    assert is_hubspot_deal_canonical("region__c") is False


def test_mapping_values_are_unique() -> None:
    values = list(HUBSPOT_DEAL_CANONICAL_FIELDS.values())
    assert len(values) == len(set(values))


def test_validator_rejects_missing_crmdeal_attr() -> None:
    with pytest.raises(ValueError, match="does not exist on CRMDeal"):
        _validate_mapping({"some_prop": "definitely_not_a_column"})


def test_validator_rejects_uppercase_key() -> None:
    with pytest.raises(ValueError, match="lowercase"):
        _validate_mapping({"DealName": "name"})


def test_validator_rejects_duplicate_value() -> None:
    with pytest.raises(ValueError, match="Duplicate canonical column"):
        _validate_mapping({"dealname": "name", "alt_name": "name"})
```

- [ ] **Step 2: Unit test the classifier**

Create `tests/service/integrations/crm/schema/test_classifier.py`:

```python
import pytest

from src.service.integrations.crm.schema.classifier import (
    MalformedHubSpotProperty,
    classify,
)
from src.service.integrations.crm.schema.schema_types import (
    CanonicalMatch,
    ProviderCustomMatch,
)


def test_canonical_property_returns_canonical_match() -> None:
    result = classify({"name": "dealname", "hubspotDefined": True})
    assert isinstance(result, CanonicalMatch)
    assert result.canonical_column == "name"


def test_hubspot_builtin_noncanonical_returns_hubspot_defined_custom() -> None:
    result = classify({"name": "hs_num_associated_contacts", "hubspotDefined": True})
    assert isinstance(result, ProviderCustomMatch)
    assert result.is_provider_custom is False


def test_tenant_custom_returns_tenant_custom() -> None:
    result = classify({"name": "region__c", "hubspotDefined": False})
    assert isinstance(result, ProviderCustomMatch)
    assert result.is_provider_custom is True


def test_missing_hubspot_defined_flag_defaults_to_tenant_custom() -> None:
    result = classify({"name": "mystery_field"})
    assert isinstance(result, ProviderCustomMatch)
    assert result.is_provider_custom is True


def test_missing_name_raises() -> None:
    with pytest.raises(MalformedHubSpotProperty):
        classify({"label": "Missing Name"})
```

- [ ] **Step 3: Unit test the normalizer and type table**

Create `tests/service/integrations/crm/schema/test_normalizer.py`:

```python
from datetime import UTC, datetime

from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
)
from src.service.integrations.crm.schema.normalizer import (
    map_hubspot_field_type,
    normalize,
    options_payload,
)
from src.service.integrations.crm.schema.schema_types import ProviderCustomMatch


def _match(name: str = "region__c", is_custom: bool = True) -> ProviderCustomMatch:
    return ProviderCustomMatch(hubspot_property=name, is_provider_custom=is_custom)


def test_currency_number_maps_to_currency() -> None:
    raw = {"type": "number", "fieldType": "number", "numberDisplayHint": "currency"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.CURRENCY


def test_percentage_number_maps_to_percentage() -> None:
    raw = {"type": "number", "fieldType": "number", "numberDisplayHint": "percentage"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.PERCENTAGE


def test_checkbox_enum_maps_to_multiselect() -> None:
    raw = {"type": "enumeration", "fieldType": "checkbox"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.MULTISELECT


def test_select_enum_maps_to_select() -> None:
    raw = {"type": "enumeration", "fieldType": "select"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.SELECT


def test_calculated_with_formula_maps_to_formula() -> None:
    raw = {"type": "string", "calculated": True, "calculationFormula": "CONCAT(x, y)"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.FORMULA


def test_calculated_without_formula_falls_back_to_text() -> None:
    raw = {"type": "string", "calculated": True}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.TEXT


def test_unknown_type_falls_back_to_text() -> None:
    raw = {"type": "weird", "fieldType": "weird"}
    assert map_hubspot_field_type(raw) == CRMCustomFieldDataType.TEXT


def test_normalize_preserves_label_and_description() -> None:
    raw = {
        "name": "region__c",
        "label": "Region",
        "description": "Geographic region",
        "type": "string",
        "fieldType": "text",
        "updatedAt": "2026-04-01T10:00:00Z",
    }
    nf = normalize(raw, _match())
    assert nf.label == "Region"
    assert nf.description == "Geographic region"
    assert nf.data_type == CRMCustomFieldDataType.TEXT
    assert nf.source_modified_at == datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
    assert nf.provider_field_key == "region__c"
    assert nf.is_provider_custom is True


def test_normalize_options_become_structured() -> None:
    raw = {
        "name": "deal_source",
        "label": "Deal Source",
        "type": "enumeration",
        "fieldType": "select",
        "options": [
            {"value": "inbound", "label": "Inbound", "displayOrder": 1, "hidden": False},
            {"value": "outbound", "label": "Outbound", "displayOrder": 2, "hidden": True},
        ],
    }
    nf = normalize(raw, _match(name="deal_source"))
    payload = options_payload(nf.options)
    assert payload["source"] == "hubspot"
    assert payload["values"][0]["value"] == "inbound"
    assert payload["values"][1]["hidden"] is True


def test_normalize_label_falls_back_to_name() -> None:
    raw = {"name": "no_label_field", "type": "string"}
    nf = normalize(raw, _match(name="no_label_field"))
    assert nf.label == "no_label_field"
```

- [ ] **Step 4: Integration-test the fetcher with httpx mocks**

Create `tests/workers/integrations_worker/providers/crm/hubspot/test_schema_fetcher.py`:

```python
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.schema_fetcher import (
    HubSpotSchemaFetcher,
)


def _token_info() -> IntegrationTokenInfo:
    return IntegrationTokenInfo(
        access_token="tok",
        refresh_token="refresh",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        integration_type=IntegrationType.HUBSPOT,
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )


def _mock_httpx_get(status_code: int, json_body: dict) -> AsyncMock:
    client = AsyncMock()
    response = httpx.Response(
        status_code=status_code,
        json=json_body,
        request=httpx.Request("GET", "https://api.hubapi.com/crm/v3/properties/deals"),
    )
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_fetch_properties_returns_results_list() -> None:
    client = _mock_httpx_get(200, {"results": [{"name": "dealname"}, {"name": "region__c"}]})
    with patch("httpx.AsyncClient", return_value=client):
        rows = await HubSpotSchemaFetcher(token_info=_token_info()).fetch_properties(
            object_type="deals"
        )
    assert [r["name"] for r in rows] == ["dealname", "region__c"]
    client.get.assert_awaited_once()
    _, kwargs = client.get.await_args
    assert kwargs["params"] == {"archived": "false"}


@pytest.mark.asyncio
async def test_fetch_properties_raises_on_non_list_results() -> None:
    client = _mock_httpx_get(200, {"results": "not-a-list"})
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="non-list results"):
            await HubSpotSchemaFetcher(token_info=_token_info()).fetch_properties(
                object_type="deals"
            )


@pytest.mark.asyncio
async def test_fetch_properties_raises_on_http_error() -> None:
    client = _mock_httpx_get(500, {"error": "boom"})
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(httpx.HTTPStatusError):
            await HubSpotSchemaFetcher(token_info=_token_info()).fetch_properties(
                object_type="deals"
            )
```

- [ ] **Step 5: Component-test the registry writer**

Create `tests/service/integrations/crm/schema/test_registry_writer.py`:

```python
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.service.integrations.crm.schema.registry_writer import (
    HubSpotDealRegistryWriter,
)
from src.service.integrations.crm.schema.schema_types import NormalizedSchemaField


def _field(key: str) -> NormalizedSchemaField:
    return NormalizedSchemaField(
        provider_field_key=key,
        key=key,
        label=key,
        description=None,
        data_type=CRMCustomFieldDataType.TEXT,
        is_provider_custom=True,
        source_modified_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_insert_new_provider_row(org_db_session, seed_org) -> None:
    writer = HubSpotDealRegistryWriter(
        session=org_db_session,
        organisation_id=seed_org.organisation_id,
        integration_id=uuid4(),
    )
    result = await writer.apply(fields=[_field("region__c")])
    assert result.created == 1
    row = result.rows[0]
    assert row.field_source == CRMCustomFieldSource.PROVIDER
    assert row.is_active is True


@pytest.mark.asyncio
async def test_update_existing_active_row_bumps_updated_not_reactivated(
    org_db_session, seed_provider_row_active
) -> None:
    row = seed_provider_row_active
    writer = HubSpotDealRegistryWriter(
        session=org_db_session,
        organisation_id=row.organisation_id,
        integration_id=row.integration_id,
    )
    nf = _field(row.provider_field_key)
    nf.label = "Updated Label"
    result = await writer.apply(fields=[nf])
    assert result.updated == 1
    assert result.reactivated == 0


@pytest.mark.asyncio
async def test_inactive_row_is_reactivated(
    org_db_session, seed_provider_row_inactive
) -> None:
    row = seed_provider_row_inactive
    writer = HubSpotDealRegistryWriter(
        session=org_db_session,
        organisation_id=row.organisation_id,
        integration_id=row.integration_id,
    )
    result = await writer.apply(fields=[_field(row.provider_field_key)])
    assert result.reactivated == 1
    assert result.updated == 0


@pytest.mark.asyncio
async def test_missing_row_is_deactivated(
    org_db_session, seed_provider_row_active
) -> None:
    row = seed_provider_row_active
    writer = HubSpotDealRegistryWriter(
        session=org_db_session,
        organisation_id=row.organisation_id,
        integration_id=row.integration_id,
    )
    result = await writer.apply(fields=[])
    assert result.deactivated == 1


@pytest.mark.asyncio
async def test_does_not_touch_internal_rows(
    org_db_session, seed_internal_row_active
) -> None:
    row = seed_internal_row_active
    writer = HubSpotDealRegistryWriter(
        session=org_db_session,
        organisation_id=row.organisation_id,
        integration_id=uuid4(),
    )
    await writer.apply(fields=[])
    await org_db_session.refresh(row)
    assert row.is_active is True
    assert row.field_source == CRMCustomFieldSource.INTERNAL
```

> Note to the engineer: `org_db_session`, `seed_org`, `seed_provider_row_active`, `seed_provider_row_inactive`, `seed_internal_row_active` are fixture names that the project's existing `tests/conftest.py` conventions should provide. If equivalent fixtures already exist under different names (check other `tests/service/integrations/crm/` tests), use those names and adjust imports. Do not invent new fixture infrastructure — reuse whatever pattern the repo already uses for organisation-scoped in-memory sessions.

- [ ] **Step 6: Component-test the service orchestrator**

Create `tests/service/integrations/crm/schema/test_schema_sync_service.py`:

```python
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.crm.schema.schema_sync_service import (
    HubSpotDealSchemaSyncService,
    SchemaSyncConfigurationError,
)


def _integration(org_id, int_id, active: bool = True, int_type=IntegrationType.HUBSPOT):
    return KnowledgeIntegration(
        integration_id=int_id,
        organisation_id=org_id,
        integration_type=int_type,
        is_active=active,
        credentials={"access_token": "a", "refresh_token": "r"},
        connection_metadata={},
        name="HubSpot",
        connection_id="c",
        created_by_user_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_run_persists_only_noncanonical_fields(org_db_session, seed_org) -> None:
    org_id = seed_org.organisation_id
    int_id = uuid4()

    fetched = [
        {"name": "dealname", "label": "Deal name", "type": "string", "fieldType": "text", "hubspotDefined": True},
        {"name": "amount", "label": "Amount", "type": "number", "fieldType": "number", "numberDisplayHint": "currency", "hubspotDefined": True},
        {"name": "region__c", "label": "Region", "type": "string", "fieldType": "text", "hubspotDefined": False},
        {"name": "hs_num_associated_contacts", "label": "Contacts", "type": "number", "fieldType": "number", "hubspotDefined": True},
    ]

    service = HubSpotDealSchemaSyncService()
    with (
        patch.object(service, "_load_token_info", AsyncMock()),
        patch(
            "src.service.integrations.crm.schema.schema_sync_service.HubSpotSchemaFetcher"
        ) as fetcher_cls,
    ):
        fetcher_cls.return_value.fetch_properties = AsyncMock(return_value=fetched)
        result = await service.run(organisation_id=org_id, integration_id=int_id)

    assert result.fetched_properties == 4
    assert result.canonical_properties == 2           # dealname, amount
    assert result.custom_properties_persisted == 2    # region__c, hs_num_associated_contacts
    assert result.created == 2
    assert result.deactivated == 0


@pytest.mark.asyncio
async def test_run_rejects_wrong_integration_type(org_db_session, seed_salesforce_integration) -> None:
    service = HubSpotDealSchemaSyncService()
    with pytest.raises(SchemaSyncConfigurationError, match="expected hubspot"):
        await service.run(
            organisation_id=seed_salesforce_integration.organisation_id,
            integration_id=seed_salesforce_integration.integration_id,
        )


@pytest.mark.asyncio
async def test_run_rejects_inactive_integration(org_db_session, seed_inactive_hubspot_integration) -> None:
    service = HubSpotDealSchemaSyncService()
    with pytest.raises(SchemaSyncConfigurationError, match="not active"):
        await service.run(
            organisation_id=seed_inactive_hubspot_integration.organisation_id,
            integration_id=seed_inactive_hubspot_integration.integration_id,
        )


@pytest.mark.asyncio
async def test_run_skips_malformed_properties(org_db_session, seed_org) -> None:
    org_id = seed_org.organisation_id
    int_id = uuid4()
    fetched = [{"label": "no-name"}, {"name": "region__c", "type": "string"}]

    service = HubSpotDealSchemaSyncService()
    with (
        patch.object(service, "_load_token_info", AsyncMock()),
        patch(
            "src.service.integrations.crm.schema.schema_sync_service.HubSpotSchemaFetcher"
        ) as fetcher_cls,
    ):
        fetcher_cls.return_value.fetch_properties = AsyncMock(return_value=fetched)
        result = await service.run(organisation_id=org_id, integration_id=int_id)

    assert result.skipped == 1
    assert result.custom_properties_persisted == 1
```

> Fixture note: `seed_salesforce_integration` and `seed_inactive_hubspot_integration` follow the same project fixture conventions as `seed_org`. If they don't exist in the repo, inline a minimal fixture that inserts a `KnowledgeIntegration` with the right state.

- [ ] **Step 7: Component-test the Tier 3 Celery task**

Create `tests/workers/integrations_worker/tasks/crm/test_hubspot_deal_schema_task.py`:

```python
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from src.service.integrations.crm.schema.schema_sync_service import (
    SchemaSyncConfigurationError,
)
from src.service.integrations.crm.schema.schema_types import SchemaSyncResult


def _stub_result(org_id, int_id) -> SchemaSyncResult:
    now = datetime.now(UTC)
    return SchemaSyncResult(
        provider="hubspot",
        object_type="deal",
        organisation_id=org_id,
        integration_id=int_id,
        fetched_properties=5,
        canonical_properties=2,
        custom_properties_persisted=3,
        created=2,
        updated=1,
        reactivated=0,
        deactivated=0,
        skipped=0,
        started_at=now,
        completed_at=now,
    )


def test_tier3_task_returns_success_payload_on_happy_path() -> None:
    from src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task import (
        sync_hubspot_deal_schema_task,
    )

    org_id = uuid4()
    int_id = uuid4()

    with (
        patch(
            "src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task.HubSpotDealSchemaSyncService"
        ) as svc_cls,
        patch(
            "src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task.SyncHistoryManager"
        ) as hist_cls,
        patch(
            "src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task.acquire_org_async_session"
        ) as acquire,
    ):
        svc_cls.return_value.run = AsyncMock(return_value=_stub_result(org_id, int_id))
        hist_cls.start_fresh = AsyncMock(return_value=uuid4())
        hist_cls.complete_fresh = AsyncMock()

        session = AsyncMock()
        session.__aiter__ = lambda self: self
        session.__anext__ = AsyncMock(return_value=AsyncMock())

        async def _fake_acquire(_):
            yield AsyncMock()

        acquire.side_effect = _fake_acquire

        result = sync_hubspot_deal_schema_task.__wrapped__(  # type: ignore[attr-defined]
            None,
            organisation_id=str(org_id),
            integration_id=str(int_id),
        )

    assert result["status"] == "success"
    assert result["custom_properties_persisted"] == 3


def test_tier3_task_records_configuration_failure() -> None:
    from src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task import (
        sync_hubspot_deal_schema_task,
    )

    org_id = uuid4()
    int_id = uuid4()

    with (
        patch(
            "src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task.HubSpotDealSchemaSyncService"
        ) as svc_cls,
        patch(
            "src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task.SyncHistoryManager"
        ) as hist_cls,
        patch(
            "src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task.acquire_org_async_session"
        ) as acquire,
    ):
        svc_cls.return_value.run = AsyncMock(side_effect=SchemaSyncConfigurationError("bad config"))
        hist_cls.start_fresh = AsyncMock(return_value=uuid4())
        hist_cls.complete_fresh = AsyncMock()

        async def _fake_acquire(_):
            yield AsyncMock()

        acquire.side_effect = _fake_acquire

        with pytest.raises(SchemaSyncConfigurationError):
            sync_hubspot_deal_schema_task.__wrapped__(  # type: ignore[attr-defined]
                None,
                organisation_id=str(org_id),
                integration_id=str(int_id),
            )

        hist_cls.complete_fresh.assert_called()
```

- [ ] **Step 8: Component-test the Tier 1 fan-out**

Create `tests/workers/integrations_worker/tasks/crm/test_hubspot_deal_schema_all_orgs_task.py`:

```python
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from src.domain.models.organisation_tables.sync import IntegrationType


def test_fan_out_dispatches_for_each_active_hubspot_integration() -> None:
    from src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_all_orgs_task import (
        sync_hubspot_deal_schema_all_orgs_task,
    )

    rows = [
        type("Row", (), {
            "organisation_id": uuid4(),
            "integration_id": uuid4(),
            "integration_type": IntegrationType.HUBSPOT,
            "is_active": True,
        })(),
        type("Row", (), {
            "organisation_id": uuid4(),
            "integration_id": uuid4(),
            "integration_type": IntegrationType.HUBSPOT,
            "is_active": True,
        })(),
    ]

    async def _fake_acquire():
        session = AsyncMock()
        execute_result = AsyncMock()
        execute_result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=execute_result)
        yield session

    with (
        patch(
            "src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_all_orgs_task.acquire_master_async_session",
            _fake_acquire,
        ),
        patch(
            "src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_all_orgs_task.send_integrations_task"
        ) as send,
    ):
        result = sync_hubspot_deal_schema_all_orgs_task.__wrapped__(None)  # type: ignore[attr-defined]

    assert result["status"] == "success"
    assert result["dispatched"] == 2
    assert send.call_count == 2
    for call_args in send.call_args_list:
        kwargs = call_args.kwargs
        assert kwargs["task_name"] == "integrations.sync_hubspot_deal_schema"
        assert set(kwargs["kwargs"].keys()) == {"organisation_id", "integration_id"}
```

- [ ] **Step 9: Component-test post-connect dispatch**

Create `tests/service/integrations/test_hubspot_initial_sync_dispatch.py`:

```python
from unittest.mock import patch
from uuid import uuid4

from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.initial_sync_service import InitialSyncService


def _integration(int_type: IntegrationType) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        organisation_id=uuid4(),
        integration_scope=IntegrationScope.ORGANIZATION,
        integration_type=int_type,
        name=int_type.value,
        credentials={"access_token": "a", "refresh_token": "r"},
        connection_metadata={},
        is_active=True,
        connection_id="conn",
        created_by_user_id=uuid4(),
    )


def test_apply_post_connect_state_queues_hubspot_schema_sync() -> None:
    integration = _integration(IntegrationType.HUBSPOT)
    InitialSyncService().apply_post_connect_state(integration)
    assert integration.connection_metadata["initial_sync_status"] == "queued"
    assert integration.connection_metadata["schema_sync"]["deals"]["status"] == "queued"
    assert "initial_sync_placeholder" not in integration.connection_metadata


def test_apply_post_connect_state_keeps_salesforce_placeholder() -> None:
    integration = _integration(IntegrationType.SALESFORCE)
    InitialSyncService().apply_post_connect_state(integration)
    assert integration.connection_metadata["initial_sync_status"] == "not_implemented"
    assert "initial_sync_placeholder" in integration.connection_metadata
    assert "schema_sync" not in integration.connection_metadata


def test_trigger_initial_sync_dispatches_hubspot_tier3() -> None:
    integration = _integration(IntegrationType.HUBSPOT)
    with patch(
        "src.service.integrations.initial_sync_service.send_integrations_task"
    ) as send:
        InitialSyncService().trigger_initial_sync(integration.organisation_id, integration)
    send.assert_called_once_with(
        task_name="integrations.sync_hubspot_deal_schema",
        kwargs={
            "organisation_id": str(integration.organisation_id),
            "integration_id": str(integration.integration_id),
        },
    )


def test_trigger_initial_sync_does_not_dispatch_for_salesforce() -> None:
    integration = _integration(IntegrationType.SALESFORCE)
    with patch(
        "src.service.integrations.initial_sync_service.send_integrations_task"
    ) as send:
        InitialSyncService().trigger_initial_sync(integration.organisation_id, integration)
    send.assert_not_called()
```

- [ ] **Step 10: Update the existing HubSpot OAuth test to the new metadata shape**

Open `tests/service/integrations/test_hubspot_oauth_flow.py`. Find assertions on `connection_metadata` after post-connect and replace any `initial_sync_status == "not_implemented"` or `initial_sync_placeholder` assertion with:

```python
assert created.connection_metadata["initial_sync_status"] == "queued"
assert created.connection_metadata["schema_sync"]["deals"]["status"] == "queued"
assert "initial_sync_placeholder" not in created.connection_metadata
```

- [ ] **Step 11: Run the full Task 12 suite**

```bash
uv run pytest tests/service/integrations/crm/schema tests/workers/integrations_worker/providers/crm/hubspot tests/workers/integrations_worker/tasks/crm tests/service/integrations/test_hubspot_initial_sync_dispatch.py tests/service/integrations/test_hubspot_oauth_flow.py -q
```

Expected: all tests pass. If any fixture name differs from the project's conventions, adjust imports to match the existing test-suite conventions rather than inventing new infrastructure.

- [ ] **Step 12: Run lint + type-check before commit**

Per CLAUDE.md, ruff + mypy are gate-level checks, not per-step. Run them once before committing Task 12:

```bash
uv run ruff check src/service/integrations/crm/schema src/workers/integrations_worker/providers/crm src/workers/integrations_worker/tasks/crm src/service/integrations/initial_sync_service.py tests/service/integrations/crm/schema tests/workers/integrations_worker/tasks/crm tests/workers/integrations_worker/providers/crm tests/service/integrations/test_hubspot_initial_sync_dispatch.py
uv run mypy src/service/integrations/crm/schema src/workers/integrations_worker/providers/crm/hubspot src/workers/integrations_worker/tasks/crm
```

Fix any findings inline before committing. Do not skip or silence errors with `# type: ignore` unless the repo already uses the same pattern for the same kind of issue.

- [ ] **Step 13: Commit Task 12**

```bash
git add tests/service/integrations/crm/schema tests/workers/integrations_worker/providers/crm/hubspot tests/workers/integrations_worker/tasks/crm tests/service/integrations/test_hubspot_initial_sync_dispatch.py tests/service/integrations/test_hubspot_oauth_flow.py
git commit -m "test(crm-schema): add full hubspot deal schema sync test suite"
```

---

## Self-Review Checklist

- [ ] Task 1 ships a canonical mapping with import-time self-validation that fails on missing `CRMDeal` attrs and duplicate values.
- [ ] Task 2 types `SchemaSyncResult`, `RegistryWriteResult`, `NormalizedSchemaField`, `CanonicalMatch`, `ProviderCustomMatch` match the spec's interfaces.
- [ ] Task 3 classifier returns `CanonicalMatch | ProviderCustomMatch` and raises `MalformedHubSpotProperty` on missing `name`.
- [ ] Task 4 normalizer covers every row of the HubSpot → internal type table in the spec (Section 8) and produces the `{"source": "hubspot", "values": [...]}` options shape.
- [ ] Task 5 fetcher hits `/crm/v3/properties/{object_type}?archived=false`, uses `call_with_token_retry`, and never holds a DB session.
- [ ] Task 6 registry writer scopes every statement by org + integration + entity + `field_source='provider'` + `is_deleted=false`, and the `updated` / `reactivated` counters are mutually exclusive per spec Section 9.
- [ ] Task 7 service follows the fresh-session pattern exactly — Session A for load, no session during HTTP, Session B for persist — and never opens a sync-history session.
- [ ] Task 8 Tier 3 task owns sync-history start/complete + `connection_metadata.schema_sync.deals` + `last_synced_at` updates; on configuration errors it marks history FAILED; on `SoftTimeLimitExceeded` it returns `timed_out` and records a sync failure.
- [ ] Task 9 Tier 1 task reads master only, dispatches per active HubSpot integration, and the `ScheduledJob` entry uses `0 2 */14 * *`.
- [ ] Task 10 replaces HubSpot `not_implemented` placeholder with `queued` + `schema_sync.deals.status=queued`; Salesforce retains `not_implemented`.
- [ ] Task 11 doc updates describe the implemented truth (HubSpot deal schema sync live; everything else deferred).
- [ ] Task 12 is the only place tests land, matching the spec's end-phase test policy.
- [ ] Every cross-task reference uses the same class/function/task name (no drift between tasks).
- [ ] Lint + mypy are deferred to Task 12 Step 12, matching CLAUDE.md guidance.

---

## Execution Notes for the Worker

- **Do not** run tests between Tasks 1–11. Smoke-check each module by importing it (the plan shows the exact command).
- **Do not** open a PR mid-plan unless the user explicitly asks — ship Tasks 1–12 as stacked commits on this branch, then the user reviews.
- **Do not** modify any file under `src/service/crm/internal/` or `src/api/routers/crm/`. Read-path hardening is deferred to the consumption-layer design.
- **Do not** amend any of the 13 commits. If a step needs a fix, create a follow-up commit.
- **Fixture gaps** in Task 12 are expected: when a fixture name in this plan doesn't match the repo's real name, change the plan's fixture name to match the repo; don't invent new fixture infrastructure.
- **Infra gap (intentional):** the Kubernetes CronJob manifest that triggers `integrations.sync_hubspot_deal_schema_all_orgs` lives outside this repo. `scheduled_jobs.py` is the source-of-truth metadata; ops applies the CronJob once the plan is merged.
