# API Response Pattern - Production Architecture

## Overview

This document defines the standard pattern for API responses in our FastAPI application, ensuring type safety, security, and maintainability.

## The Problem

When using FastAPI with SQLAlchemy, there's a common architectural tension:

```
SQLAlchemy ORM Model (DealStakeholder)
        ↓
    Service Layer
        ↓
    Handler Layer
        ↓
Pydantic Response Model (StakeholderDetail)
        ↓
    JSON Response
```

**Issues with naive approach:**

1. **Type Mismatch**: Service returns `DealStakeholder`, but API declares `StakeholderDetail`
2. **Security Risk**: ORM models may contain fields not meant for API exposure
3. **Implicit Conversion**: Relying on FastAPI's `response_model` to convert bypasses static type checking
4. **Data Leakage**: Internal fields (passwords, internal IDs) could leak if ORM model has extra fields

## Production-Ready Solution: Explicit DTO Conversion

### Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Handler Layer                                │
│  - Receives Pydantic DTOs from service                              │
│  - Returns GenericResponse[DTO]                                     │
│  - Full type safety with static analysis                            │
└─────────────────────────────────────────────────────────────────────┘
                                ↑
                    Pydantic DTOs (StakeholderDetail)
                                ↑
┌─────────────────────────────────────────────────────────────────────┐
│                         Service Layer                                │
│  - Queries database (returns ORM models)                            │
│  - Converts ORM → Pydantic DTO before returning                     │
│  - Single point of conversion (DRY)                                 │
│  - Controls exactly what fields are exposed                         │
└─────────────────────────────────────────────────────────────────────┘
                                ↑
                    SQLAlchemy ORM Models (DealStakeholder)
                                ↑
┌─────────────────────────────────────────────────────────────────────┐
│                       Repository/Database                            │
└─────────────────────────────────────────────────────────────────────┘
```

### Why Service Layer Conversion?

| Aspect | Handler Conversion | Service Conversion (Recommended) |
|--------|-------------------|----------------------------------|
| **DRY** | Repeated in every handler | Single conversion point |
| **Testing** | Must test conversion in handler tests | Service tests cover conversion |
| **Reusability** | Other services can't reuse | Other services get DTOs directly |
| **Separation** | Handler knows about ORM | Handler only knows DTOs |

### Implementation Pattern

#### 1. Pydantic DTO Model (schemas)

```python
# src/api/schemas/crm_models.py
class StakeholderDetail(BaseModel):
    """API response model - only exposes safe fields."""
    stakeholder_id: UUID
    deal_id: UUID
    email: str
    name: str | None = None
    stance: StakeholderStance
    # ... other public fields

    model_config = {"from_attributes": True}  # Enable ORM conversion
```

#### 2. Service Layer (converts ORM → DTO)

```python
# src/service/crm/stakeholders_service.py
from src.api.schemas.crm_models import StakeholderDetail

class StakeholdersService:
    async def list_stakeholders_by_deal(
        self,
        deal_id: UUID,
        **filters
    ) -> list[StakeholderDetail]:  # Returns Pydantic DTO
        """List stakeholders for a deal.

        Returns:
            List of StakeholderDetail DTOs (not ORM models)
        """
        # Query returns ORM models
        orm_models = await self._query_stakeholders(deal_id, **filters)

        # Convert to DTOs - explicit control over exposed fields
        return [
            StakeholderDetail.model_validate(s, from_attributes=True)
            for s in orm_models
        ]
```

#### 3. Handler Layer (type-safe)

```python
# src/api/routers/crm/stakeholders_handler.py
from fastapi import Response as FastAPIResponse
from src.api.utils.http_response import Response

@stakeholders_router.get(
    "/{deal_id}/stakeholders",
    response_model=GenericResponse[list[StakeholderDetail]],
)
async def get_deal_stakeholders(
    deal_id: UUID,
    response: FastAPIResponse,
    service: StakeholdersService = Depends(get_stakeholders_service),
) -> GenericResponse[list[StakeholderDetail]]:  # Type-safe!
    try:
        # Service returns DTOs - no conversion needed
        stakeholders = await service.list_stakeholders_by_deal(deal_id)
        return Response.success_response(
            message="Stakeholders retrieved",
            data=stakeholders,  # Already StakeholderDetail DTOs
        )
    except ServiceError as e:
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
            response=response,
        )
```

## Security Benefits

### 1. Explicit Field Control

```python
# ORM Model - has internal fields
class DealStakeholder(Base):
    stakeholder_id: UUID
    deal_id: UUID
    email: str
    internal_score: float      # Internal use only
    ml_embedding: bytes        # Never expose
    created_by_task_id: UUID   # Internal reference
```

```python
# DTO - only public fields
class StakeholderDetail(BaseModel):
    stakeholder_id: UUID
    deal_id: UUID
    email: str
    # internal_score: NOT included
    # ml_embedding: NOT included
    # created_by_task_id: NOT included
```

### 2. Validation Before Response

```python
# Pydantic validates during conversion
StakeholderDetail.model_validate(orm_model, from_attributes=True)
# If ORM data is invalid, raises ValidationError BEFORE sending response
```

### 3. No Accidental Data Leakage

With implicit `response_model` conversion:

- FastAPI converts at the last moment
- If conversion fails, might leak partial data
- Hard to test what gets exposed

With explicit service conversion:

- Conversion happens in service layer
- Easy to unit test
- Clear contract of what's exposed

## Response Structure

All API responses use `GenericResponse[T]`:

```python
class GenericResponse(BaseModel, Generic[T]):
    status: str      # "SUCCESS" or "ERROR"
    message: str     # Human-readable message
    data: T | None   # Typed payload
```

### Convenience Response Helper (Optional, Safe)

Use `src/api/utils/http_response.py` to standardize success/error responses without
returning `JSONResponse` directly (which bypasses `response_model` validation).

Rules:

- The helper returns `GenericResponse` (not `JSONResponse`).
- Always pass `response=` when setting non-200 HTTP status codes.
- Optional: pass `data_model=` to validate `data` before returning.

```python
from fastapi import Response as FastAPIResponse
from src.api.utils.http_response import Response

async def handler(response: FastAPIResponse):
    if error:
        return Response.error_response(
            message="Not found",
            status_code=404,
            response=response,
        )
    return Response.success_response(message="OK", data=result)
```

### Success Response

```json
{
    "status": "SUCCESS",
    "message": "Stakeholders retrieved",
    "data": [
        {"stakeholder_id": "...", "email": "...", ...}
    ]
}
```

### Error Response

```json
{
    "status": "ERROR",
    "message": "Stakeholder not found",
    "data": null
}
```

### HTTP Status Codes

Use `Response` parameter to set status codes. If using the helper, pass `response=`.

```python
async def handler(response: Response):
    if error:
        return Response.error_response(
            message="Not found",
            status_code=404,
            response=response,
        )
    return Response.success_response(message="OK", data=result)
```

## Migration Path

For existing services returning ORM models:

1. **Add DTO conversion to service methods** (one-time)
2. **Update return type annotations**
3. **Run tests** to verify no data leakage
4. **Update handlers** to use type-safe pattern

## Checklist for New Endpoints

- [ ] Define Pydantic DTO in `schemas/` with only public fields
- [ ] Service method converts ORM → DTO before returning
- [ ] Handler returns `GenericResponse[DTO]` with proper type annotation
- [ ] Error cases set HTTP status via `response.status_code` (or `response=` in helper)
- [ ] Both success and error return `GenericResponse`
- [ ] Avoid returning `JSONResponse` directly from handlers
- [ ] Unit tests verify only expected fields are in response

## References

- [FastAPI Response Model](https://fastapi.tiangolo.com/tutorial/response-model/)
- [Pydantic ORM Mode](https://docs.pydantic.dev/latest/concepts/models/#model-methods-and-properties)
- [DTO Pattern](https://martinfowler.com/eaaCatalog/dataTransferObject.html)
