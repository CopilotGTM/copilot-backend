"""Deal Notes API handler - endpoints for managing deal notes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_note_models import (
    CreateNoteRequest,
    DealNoteItem,
    DealNotesPage,
    DealParticipant,
    NoteAuthor,
    NoteMetadata,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.crm import CRMActivity
from src.infra.utils.logger import get_logger
from src.service.crm.deal_notes_service import (
    DealNotesService,
    DealNotesServiceError,
)

logger = get_logger("deal_notes_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

deal_notes_router = APIRouter()


async def get_deal_notes_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a DealNotesService instance for the current org.

    Ensures proper cleanup of database connections via try/finally pattern.
    """
    service = DealNotesService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealNotesService close failed", exc_info=True)


def _map_note_to_schema(note: CRMActivity, source: str) -> DealNoteItem:
    """Map CRMActivity to DealNoteItem schema.

    Args:
        note: CRMActivity record
        source: Determined source (user, hubspot, salesforce)

    Returns:
        DealNoteItem schema object
    """
    is_user_created = note.integration_id is None

    return DealNoteItem(
        note_id=note.activity_id,
        deal_id=note.deal_id,
        content=note.body or "",
        subject=note.subject,
        created_at=note.source_created_at or note.created_at,
        updated_at=note.synced_at,
        source=source,
        is_user_created=is_user_created,
        author=NoteAuthor(
            id=note.owner_external_id or "",
            name=note.owner_name,
            email=note.owner_email,
        ),
        metadata=NoteMetadata(
            external_id=note.external_id if not is_user_created else None,
            synced_at=note.synced_at if not is_user_created else None,
        ),
    )


@deal_notes_router.get(
    "/{deal_id}/notes",
    response_model=GenericResponse[DealNotesPage],
    summary="Get deal notes",
    description="Retrieve all notes for a specific deal (CRM-synced and user-created)",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_deal_notes(
    deal_id: UUID,
    response: FastAPIResponse,
    limit: int = Query(50, ge=1, le=100, description="Maximum notes per page"),
    cursor: str | None = Query(None, description="Pagination cursor from previous response"),
    sort: str = Query("desc", pattern="^(asc|desc)$", description="Sort order by created date"),
    service: DealNotesService = Depends(get_deal_notes_service),
) -> GenericResponse[DealNotesPage]:
    """Get paginated notes for a deal.

    Returns both CRM-synced notes (from HubSpot/Salesforce) and user-created notes.
    Notes are sorted by creation date (newest first by default).

    Query parameters:
    - limit: Number of notes per page (1-100, default 50)
    - cursor: Opaque cursor for pagination
    - sort: Sort order (desc = newest first, asc = oldest first)
    """
    try:
        notes, next_cursor, total_count, participants = await service.get_deal_notes(
            deal_id=deal_id,
            limit=limit,
            cursor=cursor,
            sort_order=sort,
        )

        # Map to response schema
        items = [_map_note_to_schema(note, service.determine_note_source(note)) for note in notes]

        # Map participants to schema
        participant_list = [
            DealParticipant(
                email=p.email,
                name=p.name,
            )
            for p in participants
        ]

        return Response.success_response(
            message="Notes retrieved successfully",
            data=DealNotesPage(
                items=items,
                cursor=next_cursor,
                total_count=total_count,
                participants=participant_list,
            ),
        )

    except DealNotesServiceError as exc:
        logger.warning("Deal notes access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error getting notes for deal %s: %s", deal_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


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
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: DealNotesService = Depends(get_deal_notes_service),
) -> GenericResponse[DealNoteItem]:
    """Create a new user-generated note for a deal.

    User-created notes are stored alongside CRM-synced notes but are
    distinguished by their source. Only users in the organization that
    owns the deal can create notes.
    """
    try:
        note = await service.create_user_note(
            deal_id=deal_id,
            content=request.content,
            subject=request.subject,
            user_id=user_ids.user_id,
            user_name=user_ids.user_name,
            user_email=user_ids.user_email,
        )

        # Commit the transaction
        await service.session.commit()

        # Map to response schema
        note_schema = _map_note_to_schema(note, service.determine_note_source(note))

        return Response.created_response(
            message="Note created successfully",
            data=note_schema,
            response=response,
        )

    except DealNotesServiceError as exc:
        logger.warning("Failed to create note: %s", exc.message, exc_info=True)
        await service.session.rollback()
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error(
            "Unexpected error creating note for deal %s: %s",
            deal_id,
            exc,
            exc_info=True,
        )
        await service.session.rollback()
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
