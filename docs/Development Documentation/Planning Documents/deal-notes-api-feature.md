# Deal Notes API Feature

## Overview

This document describes the design and implementation plan for exposing **deal notes** via REST APIs to the frontend application. Notes can come from two sources:

1. **CRM-synced notes**: Notes fetched from HubSpot and Salesforce during integration syncs
2. **User-created notes**: Notes added directly by users through our application interface

Both types of notes are stored in the same database table (`crm_activities`) but are distinguished by their source and metadata.

## Background

Refer to [deal-notes.md](../Technical%20Documentation/deal/deal-notes.md) for details on how CRM notes are currently synced and stored.

### Current State

- Notes are synced from HubSpot (`GET /crm/v3/objects/notes`) and Salesforce (SOQL `Note` object)
- Notes are stored in `crm_activities` table with `activity_type = NOTE`
- Notes are linked to deals via `deal_id` and `deal_external_id`
- No frontend API exists to retrieve or create notes

## Feature Requirements

### Functional Requirements

1. **GET Notes API**: Retrieve all notes for a specific deal
   - Return both CRM-synced and user-created notes
    - Support cursor-based pagination
   - Sort by creation date (newest first)
   - Include note metadata (author, timestamps, source)

2. **POST Notes API**: Create a new user note for a deal
   - Validate user has access to the deal
   - Link note to authenticated user as author
   - Mark as user-created (not CRM-synced)
   - Return created note with full metadata

3. **Frontend Integration**: Display notes in deal detail view
   - Show note content, author, and timestamp
   - Distinguish CRM-synced vs user-created notes
   - Allow adding new notes inline

### Non-Functional Requirements

- **Security**: Enforce organization-level data isolation
- **Authorization**: Users can only access notes for deals in their organization
- **Performance**: Paginated responses for deals with many notes
- **Auditability**: Track who created user notes and when

## Database Schema

### Existing Table: `crm_activities`

The `crm_activities` table already supports notes. Key columns:

```python
class CRMActivity(SyncedRecordMixin, table=True):
    __tablename__ = "crm_activities"
    
    # Identity
    activity_id: UUID  # Primary key
    external_id: str  # Provider ID (for CRM-synced notes)
    
    # Deal linkage
    deal_id: UUID | None  # FK to crm_deals
    deal_external_id: str | None  # Provider deal ID
    
    # Activity details
    activity_type: str  # Will be "NOTE"
    subject: str | None  # Note title (if any)
    body: str | None  # Note content (main field)
    
    # Timestamps
    source_created_at: datetime | None  # When note was created in CRM
    synced_at: datetime  # When we synced it
    
    # Ownership
    owner_external_id: str | None  # CRM user ID
    owner_name: str | None  # CRM user name
    owner_email: str | None  # CRM user email
    
    # Metadata
    properties: dict[str, Any]  # Provider-specific fields
    data_payload: dict | None  # Raw provider data
    
    # Organization scoping
    organisation_id: UUID  # Tenant isolation
    integration_id: UUID | None  # Which CRM integration
```

### Schema Enhancements Needed

To distinguish user-created notes from CRM-synced notes, we need to track:

1. **Source type**: Is this note from a CRM or from a user?
2. **User attribution**: Who created this note (for user-created notes)?

**Proposed schema additions:**

```python
class CRMActivity(SyncedRecordMixin, table=True):
    # ... existing fields ...
    
    # New fields for user-created notes
    is_user_created: bool = Field(
        default=False,
        description="True if this note was created by a user, not synced from CRM"
    )
    
    created_by_user_id: UUID | None = Field(
        default=None,
        foreign_key="organisation_users.user_id",
        description="User who created this note (for user-created notes only)"
    )
    
    created_by_name: str | None = Field(
        default=None,
        description="Name of user who created this note"
    )
    
    created_by_email: str | None = Field(
        default=None,
        description="Email of user who created this note"
    )
```

**Alternative approach (no schema changes):**

We can distinguish user-created notes without adding columns:

- **CRM-synced notes**: Have `integration_id` set, `external_id` is provider ID
- **User-created notes**: `integration_id` is NULL, `external_id` is our generated UUID
- For user notes, use `owner_external_id` to store user_id
- Use `owner_name` and `owner_email` for user attribution

This avoids migrations and reuses existing columns. **Recommended for MVP.**

## API Design

### Endpoint 1: GET Deal Notes

**Endpoint:** `GET /users/crm/deals/{deal_id}/notes`

**Purpose:** Retrieve all notes for a specific deal

**Authentication:** Required (JWT token)

**Authorization:** User must belong to the organization that owns the deal

**Path Parameters:**
- `deal_id` (UUID): The internal deal ID

**Query Parameters:**
- `limit` (int, default=50, max=100): Maximum notes per page
- `cursor` (string, optional): Opaque cursor from a previous response
- `sort` (str, default="desc"): Sort order (`asc`, `desc`) by created timestamp

**Response Schema (actual implementation):**

```json
{
  "success": true,
  "data": {
        "items": [
      {
        "note_id": "uuid",
        "deal_id": "uuid",
        "content": "Note body text",
        "subject": "Note title (optional)",
        "created_at": "2025-01-15T10:30:00Z",
        "updated_at": "2025-01-15T10:30:00Z",
        "source": "user" | "hubspot" | "salesforce",
        "is_user_created": true,
        "author": {
          "id": "user_id or crm_user_id",
          "name": "John Doe",
          "email": "john@example.com"
        },
        "metadata": {
          "external_id": "provider_note_id (if CRM-synced)",
          "synced_at": "2025-01-15T10:30:00Z (if CRM-synced)"
        }
      }
    ],
        "cursor": "opaque-cursor-or-null",
        "total_count": 127
  }
}
```

**Error Responses:**
- `404 Not Found`: Deal does not exist or user has no access
- `403 Forbidden`: User's organization does not own this deal

### Endpoint 2: POST Create Note

**Endpoint:** `POST /users/crm/deals/{deal_id}/notes`

**Purpose:** Create a new user note for a deal

**Authentication:** Required (JWT token)

**Authorization:** User must belong to the organization that owns the deal

**Path Parameters:**
- `deal_id` (UUID): The internal deal ID

**Request Body:**

```json
{
  "content": "Note body text (required)",
  "subject": "Note title (optional)"
}
```

**Validation Rules:**
- `content`: Required, non-empty string, max 10,000 characters
- `subject`: Optional string, max 500 characters

**Response Schema (actual implementation):**

```json
{
  "success": true,
  "data": {
    "note": {
      "note_id": "uuid",
      "deal_id": "uuid",
      "content": "Note body text",
      "subject": "Note title",
      "created_at": "2025-01-15T10:30:00Z",
      "updated_at": "2025-01-15T10:30:00Z",
      "source": "user",
      "is_user_created": true,
      "author": {
        "id": "user_id",
        "name": "John Doe",
        "email": "john@example.com"
      }
    }
  }
}

**HTTP Status:** `201 Created`
```

**Error Responses:**
- `400 Bad Request`: Invalid request body (validation errors)
- `404 Not Found`: Deal does not exist or user has no access
- `403 Forbidden`: User's organization does not own this deal

## Implementation Plan

### Phase 1: Database & Models (No Migration Needed)

**File:** `src/domain/models/organisation_tables/sync/crm/activities.py`

No changes needed to the model. We'll use existing columns:
- `integration_id = NULL` → indicates user-created note
- `external_id` → generated UUID for user notes
- `owner_external_id` → user_id (string conversion)
- `owner_name`, `owner_email` → user details

### Phase 2: API Schemas

**File:** `src/api/schemas/crm_models.py` (create/update)

```python
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field, validator

class NoteAuthor(BaseModel):
    """Note author information"""
    id: str = Field(description="User ID or CRM user ID")
    name: str | None = Field(description="Author name")
    email: str | None = Field(description="Author email")

class NoteMetadata(BaseModel):
    """Additional note metadata"""
    external_id: str | None = Field(description="CRM external ID if synced")
    synced_at: datetime | None = Field(description="Last sync time if CRM note")

class DealNote(BaseModel):
    """Deal note response model"""
    note_id: UUID = Field(description="Internal note ID")
    deal_id: UUID = Field(description="Deal ID this note belongs to")
    content: str = Field(description="Note content/body")
    subject: str | None = Field(description="Note subject/title")
    created_at: datetime = Field(description="When note was created")
    updated_at: datetime | None = Field(description="When note was last updated")
    source: str = Field(description="Note source: user, hubspot, salesforce")
    is_user_created: bool = Field(description="True if created by user, not CRM")
    author: NoteAuthor = Field(description="Note author information")
    metadata: NoteMetadata | None = Field(description="Additional metadata")

class DealNotesPage(BaseModel):
    """Paginated deal notes response"""
    notes: list[DealNote] = Field(description="List of notes")
    pagination: PaginationInfo = Field(description="Pagination metadata")

class CreateNoteRequest(BaseModel):
    """Request to create a new note"""
    content: str = Field(
        min_length=1,
        max_length=10000,
        description="Note content (required)"
    )
    subject: str | None = Field(
        None,
        max_length=500,
        description="Note subject/title (optional)"
    )

    @validator('content')
    def content_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError('Content cannot be empty')
        return v.strip()

class CreateNoteResponse(BaseModel):
    """Response after creating a note"""
    success: bool = True
    data: dict[str, DealNote] = Field(description="Created note")
```

### Phase 3: Service Layer

**File:** `src/service/crm/deal_notes_service.py` (new file)

```python
"""Service for managing deal notes (CRM-synced and user-created)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.activities import (
    CRMActivity,
    CRMActivityType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class DealNotesServiceError(Exception):
    """Base exception for deal notes service errors."""
    pass


class DealNotesService:
    """Service for managing deal notes."""

    def __init__(self, organisation_id: UUID, session: AsyncSession):
        self.organisation_id = organisation_id
        self.session = session

    async def get_deal_notes(
        self,
        deal_id: UUID,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[CRMActivity], int]:
        """
        Retrieve notes for a specific deal with pagination.

        Args:
            deal_id: Internal deal UUID
            page: Page number (1-indexed)
            page_size: Number of notes per page
            sort_by: Field to sort by (created_at, updated_at)
            sort_order: Sort order (asc, desc)

        Returns:
            Tuple of (notes list, total count)

        Raises:
            DealNotesServiceError: If deal not found or access denied
        """
        # Verify deal exists and belongs to this organization
        deal_query = select(CRMDeal).where(
            CRMDeal.organisation_id == self.organisation_id,
            CRMDeal.deal_id == deal_id,
        )
        result = await self.session.execute(deal_query)
        deal = result.scalar_one_or_none()

        if not deal:
            raise DealNotesServiceError(f"Deal {deal_id} not found or access denied")

        # Build notes query
        notes_query = (
            select(CRMActivity)
            .where(
                CRMActivity.organisation_id == self.organisation_id,
                CRMActivity.deal_id == deal_id,
                CRMActivity.activity_type == CRMActivityType.NOTE,
            )
        )

        # Count total
        count_query = select(func.count()).select_from(CRMActivity).where(
            CRMActivity.organisation_id == self.organisation_id,
            CRMActivity.deal_id == deal_id,
            CRMActivity.activity_type == CRMActivityType.NOTE,
        )
        count_result = await self.session.execute(count_query)
        total_count = count_result.scalar() or 0

        # Apply sorting
        sort_column = (
            CRMActivity.source_created_at
            if sort_by == "created_at"
            else CRMActivity.synced_at
        )
        if sort_order == "desc":
            notes_query = notes_query.order_by(sort_column.desc())
        else:
            notes_query = notes_query.order_by(sort_column.asc())

        # Apply pagination
        offset = (page - 1) * page_size
        notes_query = notes_query.offset(offset).limit(page_size)

        # Execute query
        result = await self.session.execute(notes_query)
        notes = result.scalars().all()

        return list(notes), total_count

    async def create_user_note(
        self,
        deal_id: UUID,
        content: str,
        subject: str | None,
        user_id: UUID,
        user_name: str | None,
        user_email: str | None,
    ) -> CRMActivity:
        """
        Create a new user-generated note for a deal.

        Args:
            deal_id: Internal deal UUID
            content: Note content/body
            subject: Note subject/title (optional)
            user_id: ID of user creating the note
            user_name: Name of user creating the note
            user_email: Email of user creating the note

        Returns:
            Created CRMActivity note record

        Raises:
            DealNotesServiceError: If deal not found or access denied
        """
        # Verify deal exists and belongs to this organization
        deal_query = select(CRMDeal).where(
            CRMDeal.organisation_id == self.organisation_id,
            CRMDeal.deal_id == deal_id,
        )
        result = await self.session.execute(deal_query)
        deal = result.scalar_one_or_none()

        if not deal:
            raise DealNotesServiceError(f"Deal {deal_id} not found or access denied")

        # Create note activity
        now = datetime.utcnow()
        note = CRMActivity(
            activity_id=uuid4(),
            organisation_id=self.organisation_id,
            integration_id=None,  # NULL indicates user-created
            external_id=str(uuid4()),  # Generate unique ID
            activity_type=CRMActivityType.NOTE,
            subject=subject,
            body=content,
            deal_id=deal_id,
            deal_external_id=deal.external_id,
            owner_external_id=str(user_id),  # Store user ID as string
            owner_name=user_name,
            owner_email=user_email,
            source_created_at=now,
            synced_at=now,
            properties={
                "is_user_created": True,
                "created_via": "api",
            },
        )

        self.session.add(note)
        await self.session.flush()
        await self.session.refresh(note)

        logger.info(
            f"Created user note {note.activity_id} for deal {deal_id} "
            f"by user {user_id}"
        )

        return note

    def _determine_note_source(self, note: CRMActivity) -> str:
        """Determine the source of a note (user, hubspot, salesforce)."""
        if note.integration_id is None:
            return "user"
        
        # Check integration provider_name if available
        # For now, use simple heuristics based on external_id format
        if note.external_id and note.external_id.startswith("00"):
            return "salesforce"  # Salesforce IDs start with 00
        
        return "hubspot"  # Default to HubSpot for CRM-synced notes
```

### Phase 4: API Router

**File:** `src/api/routers/crm/deal_notes_handler.py` (new file)

```python
"""Deal Notes API handler - endpoints for managing deal notes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.crm_models import (
    CreateNoteRequest,
    CreateNoteResponse,
    DealNote,
    DealNotesPage,
    NoteAuthor,
    NoteMetadata,
)
from src.api.schemas.base_models import PaginationInfo
from src.infra.utils.logger import get_logger
from src.service.crm.deal_notes_service import (
    DealNotesService,
    DealNotesServiceError,
)

logger = get_logger("deal_notes_handler")

deal_notes_router = APIRouter()


def _map_note_to_schema(note, source: str) -> DealNote:
    """Map CRMActivity to DealNote schema."""
    return DealNote(
        note_id=note.activity_id,
        deal_id=note.deal_id,
        content=note.body or "",
        subject=note.subject,
        created_at=note.source_created_at or note.synced_at,
        updated_at=note.synced_at,
        source=source,
        is_user_created=(note.integration_id is None),
        author=NoteAuthor(
            id=note.owner_external_id or "",
            name=note.owner_name,
            email=note.owner_email,
        ),
        metadata=NoteMetadata(
            external_id=note.external_id if note.integration_id else None,
            synced_at=note.synced_at if note.integration_id else None,
        ),
    )


@deal_notes_router.get(
    "/{deal_id}/notes",
    response_model=GenericResponse[DealNotesPage],
    summary="Get deal notes",
    description="Retrieve all notes for a specific deal (CRM-synced and user-created)",
)
async def get_deal_notes(
    deal_id: UUID,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=100, description="Notes per page"),
    sort_by: str = Query("created_at", regex="^(created_at|updated_at)$"),
    sort_order: str = Query("desc", regex="^(asc|desc)$"),
    user: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Get paginated notes for a deal."""
    try:
        service = DealNotesService(user.organisation_id, session)
        notes, total_count = await service.get_deal_notes(
            deal_id=deal_id,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )

        # Map to response schema
        note_schemas = [
            _map_note_to_schema(note, service._determine_note_source(note))
            for note in notes
        ]

        return DealNotesPage(
            notes=note_schemas,
            pagination=PaginationInfo(
                page=page,
                page_size=page_size,
                total_count=total_count,
                total_pages=(total_count + page_size - 1) // page_size,
            ),
        )

    except DealNotesServiceError as e:
        logger.warning(f"Deal notes access error: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error getting deal notes: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@deal_notes_router.post(
    "/{deal_id}/notes",
    response_model=GenericResponse[DealNoteItem],
    status_code=201,
    summary="Create deal note",
    description="Create a new user note for a deal",
)
async def create_deal_note(
    deal_id: UUID,
    request: CreateNoteRequest,
    user: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Create a new note for a deal."""
    try:
        service = DealNotesService(user.organisation_id, session)
        note = await service.create_user_note(
            deal_id=deal_id,
            content=request.content,
            subject=request.subject,
            user_id=user.user_id,
            user_name=user.user_name,
            user_email=user.user_email,
        )

        await session.commit()

        note_schema = _map_note_to_schema(
            note, service._determine_note_source(note)
        )

        return CreateNoteResponse(
            success=True,
            data={"note": note_schema},
        )

    except DealNotesServiceError as e:
        logger.warning(f"Failed to create note: {e}")
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error creating note: {e}", exc_info=True)
        await session.rollback()
        raise HTTPException(status_code=500, detail="Internal server error")
```

### Phase 5: Router Integration

**File:** `src/api/routers/crm/__init__.py`

```python
from .deal_notes_handler import deal_notes_router

__all__ = [
    # ... existing exports ...
    "deal_notes_router",
]
```

**File:** `src/api/fastapi_app.py`

```python
from src.api.routers.crm import (
    # ... existing imports ...
    deal_notes_router,
)

# In the app initialization:
fastapp.include_router(
    deal_notes_router,
    prefix="/users/crm/deals",
    tags=["deal-notes"]
)
```

## Frontend Integration

### API Client Implementation

```typescript
// types/deal-notes.ts
export interface NoteAuthor {
  id: string;
  name: string | null;
  email: string | null;
}

export interface NoteMetadata {
  external_id: string | null;
  synced_at: string | null;
}

export interface DealNote {
  note_id: string;
  deal_id: string;
  content: string;
  subject: string | null;
  created_at: string;
  updated_at: string | null;
  source: 'user' | 'hubspot' | 'salesforce';
  is_user_created: boolean;
  author: NoteAuthor;
  metadata: NoteMetadata | null;
}

export interface DealNotesPage {
  notes: DealNote[];
  pagination: {
    page: number;
    page_size: number;
    total_count: number;
    total_pages: number;
  };
}

export interface CreateNoteRequest {
  content: string;
  subject?: string;
}

// api/deal-notes.ts
export async function getDealNotes(
  dealId: string,
  page: number = 1,
  pageSize: number = 50
): Promise<DealNotesPage> {
  const response = await apiClient.get(
    `/users/crm/deals/${dealId}/notes`,
    { params: { page, page_size: pageSize } }
  );
  return response.data;
}

export async function createDealNote(
  dealId: string,
  request: CreateNoteRequest
): Promise<DealNote> {
  const response = await apiClient.post(
    `/users/crm/deals/${dealId}/notes`,
    request
  );
  return response.data.data.note;
}
```

### UI Component Structure

```
DealDetail
├── DealHeader
├── DealTabs
│   ├── Overview
│   ├── Timeline
│   └── Notes  ← New tab
│       ├── NotesHeader (count, add button)
│       ├── AddNoteForm (inline composer)
│       └── NotesList
│           ├── NoteItem (user note)
│           ├── NoteItem (CRM note with badge)
│           └── NoteItem (CRM note with badge)
└── DealSidebar
```

### Display Differentiation

**User-created notes:**
- Show user avatar/initials
- Display "You" or user name
- No integration badge

**CRM-synced notes:**
- Show CRM icon (HubSpot orange, Salesforce blue)
- Display badge "Synced from HubSpot" or "Synced from Salesforce"
- Show original CRM user name
- Maybe show sync timestamp on hover

## Testing Strategy

### Unit Tests

1. **Service Layer Tests** (`tests/service/crm/test_deal_notes_service.py`):
   - Test getting notes with pagination
   - Test creating user notes
   - Test deal access validation
   - Test note source determination

2. **API Tests** (`tests/api/crm/test_deal_notes_handler.py`):
   - Test GET endpoint with various query parameters
   - Test POST endpoint with valid/invalid payloads
   - Test authorization (wrong org, non-existent deal)
   - Test pagination edge cases

### Integration Tests

1. Create a deal with both CRM-synced and user notes
2. Verify notes are returned in correct order
3. Verify pagination works correctly
4. Verify source detection is accurate

### Manual Testing Checklist

- [ ] GET notes for deal with no notes (empty list)
- [ ] GET notes for deal with mixed CRM/user notes
- [ ] POST note with valid content
- [ ] POST note with empty content (should fail)
- [ ] POST note with very long content
- [ ] POST note to non-existent deal (should 404)
- [ ] POST note to deal in different org (should 403)
- [ ] Verify pagination works with 100+ notes
- [ ] Verify sorting by created_at vs updated_at

## Security Considerations

1. **Data Isolation**: All queries must filter by `organisation_id`
2. **Authorization**: Verify user belongs to org before creating notes
3. **Input Validation**: Sanitize note content to prevent XSS
4. **Rate Limiting**: Consider rate limiting note creation (e.g., 100/hour)
5. **Audit Trail**: Log all note creation events with user_id

## Performance Considerations

1. **Indexing**: Existing indexes on `deal_id` and `activity_type` should be sufficient
2. **Pagination**: Always paginate; default page_size=50, max=100
3. **N+1 Queries**: Consider eager loading if we add relationships
4. **Caching**: Not needed for MVP; notes change infrequently enough

## Future Enhancements

### Phase 2 Features

1. **Edit/Delete Notes**: Allow users to edit/delete their own notes
2. **Rich Text**: Support markdown or rich text formatting
3. **Attachments**: Allow attaching files to notes
4. **@Mentions**: Tag team members in notes with notifications
5. **Note Templates**: Pre-defined note templates for common scenarios
6. **Search**: Full-text search across note content

### CRM Sync-Back

- **Two-way sync**: Push user-created notes back to CRM (complex!)
- Requires mapping our notes to CRM note format
- Need to handle conflicts and deduplication
- Consider as a premium feature

### Real-time Updates

- WebSocket support for live note updates
- Notify when other team members add notes to a deal
- Show "User X is typing..." indicators

## Migration Plan

### Step 1: No Database Migration Needed

We're using existing columns, so no Alembic migration required.

### Step 2: Deploy Backend

1. Deploy service layer (`deal_notes_service.py`)
2. Deploy API router (`deal_notes_handler.py`)
3. Register router in `fastapi_app.py`
4. Deploy to staging for testing

### Step 3: Deploy Frontend

1. Deploy API client functions
2. Deploy Notes tab component
3. Add to deal detail page
4. Feature flag for gradual rollout

### Step 4: Monitoring

1. Monitor API performance (response times)
2. Track note creation rate
3. Monitor for errors or access violations
4. Gather user feedback

## Documentation Updates

After implementation, update these docs:

1. **API Documentation**: Add OpenAPI specs to `docs/API Documentation/`
2. **Technical Docs**: Update deal-notes.md with user-created notes info
3. **README**: Add notes feature to feature list
4. **Changelog**: Document new endpoints and functionality

## Questions & Decisions

### Q: Should notes be editable/deletable?
**A:** Not in MVP. Add in Phase 2 based on user feedback.

### Q: Should we sync user-created notes back to CRM?
**A:** Not in MVP. This is complex and requires careful design. Consider for future.

### Q: What about note permissions (who can see notes)?
**A:** For MVP, all users in an organization can see all notes. Future: add role-based access.

### Q: Should we support @mentions or notifications?
**A:** Not in MVP. Add in Phase 2 if there's demand.

### Q: How do we handle very long notes?
**A:** Limit to 10,000 characters. Add "Read more" UI expansion if needed.

## Success Metrics

After launch, track:
1. **Adoption**: % of users who create at least one note
2. **Usage**: Average notes created per deal
3. **Performance**: API response time (target <200ms p95)
4. **Errors**: Error rate (target <1%)
5. **Feedback**: User satisfaction with notes feature

---

**Document Status:** Draft for Review  
**Created:** 2025-12-24  
**Author:** Development Team  
**Next Review:** After implementation
