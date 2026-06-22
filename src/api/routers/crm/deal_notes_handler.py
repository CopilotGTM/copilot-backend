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
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.presenters.crm.note_presenter import present_note_item, present_notes_page
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_note_models import (
    CreateNoteRequest,
    DealNoteItem,
    DealNotesPage,
    UpdateNoteRequest,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.notes.contracts import NotesServiceError, NoteWriteContext
from src.service.crm.notes.insight_dispatch_service import NotesInsightDispatchService
from src.service.crm.notes.read_service import NotesReadService
from src.service.crm.notes.write_service import NotesWriteService


class DealNotesServices:
    """Request-scoped notes services sharing one org session."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.session = org_session
        self.read = NotesReadService(org_session, organisation_id)
        self.write = NotesWriteService(org_session, organisation_id)
        self.dispatch = NotesInsightDispatchService(org_session, organisation_id)

    async def close(self) -> None:
        await self.session.close()


logger = get_logger("deal_notes_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

deal_notes_router = APIRouter()


def _note_update_payload(request: UpdateNoteRequest) -> dict[str, str | None]:
    updates = request.model_dump(exclude_unset=True)
    payload: dict[str, str | None] = {}
    if "body" in updates:
        payload["content"] = updates["body"]
    if "title" in updates:
        payload["subject"] = updates["title"]
    return payload


async def get_deal_notes_services(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields request-scoped notes services for the current org.

    Ensures proper cleanup of database connections via try/finally pattern.
    """
    service = DealNotesServices(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealNotesServices close failed", exc_info=True)


get_deal_notes_service = get_deal_notes_services


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
    service: DealNotesServices = Depends(get_deal_notes_services),
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
        notes, next_cursor, total_count, participants = await service.read.get_deal_notes(
            deal_id=deal_id,
            limit=limit,
            cursor=cursor,
            sort_order=sort,
        )

        return Response.success_response(
            message="Notes retrieved successfully",
            data=present_notes_page(
                notes=notes,
                cursor=next_cursor,
                total_count=total_count,
                participants=participants,
            ),
        )

    except NotesServiceError as exc:
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
    service: DealNotesServices = Depends(get_deal_notes_services),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealNoteItem]:
    """Create a new user-generated note for a deal.

    User-created notes are stored alongside CRM-synced notes but are
    distinguished by their source. Only users in the organization that
    owns the deal can create notes.
    """
    try:
        create_result = await service.write.create_user_note(
            deal_id=deal_id,
            content=request.body,
            subject=request.title,
            context=NoteWriteContext(
                source="api",
                actor_user_id=user_ids.user_id,
                actor_name=user_ids.user_name,
                actor_email=user_ids.user_email,
                created_via="api",
            ),
        )

        # Commit the transaction
        await service.session.commit()

        note_schema = present_note_item(create_result.note)

        if create_result.created_new:
            # Trigger deal memory update after note creation
            await service.dispatch.trigger_deal_insight_pipeline(
                deal_id=deal_id,
                user_id=user_ids.user_id,
                trigger_source="note_creation",
                trigger_source_id=create_result.note.note_id,
            )

            return Response.created_response(
                message="Note created successfully",
                data=note_schema,
                response=response,
            )

        return Response.success_response(
            message="Identical note already exists; duplicate was not created",
            data=note_schema,
            response=response,
        )

    except NotesServiceError as exc:
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


@deal_notes_router.patch(
    "/{deal_id}/notes/{note_id}",
    response_model=GenericResponse[DealNoteItem],
    summary="Update deal note",
    description="Update a user-created note for a deal",
)
async def update_deal_note(
    deal_id: UUID,
    note_id: UUID,
    request: UpdateNoteRequest,
    response: FastAPIResponse,
    service: DealNotesServices = Depends(get_deal_notes_services),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealNoteItem]:
    """Update a user-created note for a deal."""
    try:
        note = await service.write.update_user_note(
            deal_id=deal_id,
            note_id=note_id,
            payload=_note_update_payload(request),
        )
        await service.session.commit()
        note_schema = present_note_item(note)
        return Response.success_response(message="Note updated successfully", data=note_schema)
    except NotesServiceError as exc:
        await service.session.rollback()
        logger.warning("Failed to update note: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error(
            "Unexpected error updating note %s for deal %s: %s",
            note_id,
            deal_id,
            exc,
            exc_info=True,
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_notes_router.delete(
    "/{deal_id}/notes/{note_id}",
    response_model=GenericResponse[DealNoteItem],
    summary="Delete deal note",
    description="Soft delete a user-created note for a deal",
)
async def delete_deal_note(
    deal_id: UUID,
    note_id: UUID,
    response: FastAPIResponse,
    service: DealNotesServices = Depends(get_deal_notes_services),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealNoteItem]:
    """Soft delete a user-created note for a deal."""
    try:
        note = await service.write.delete_user_note(deal_id=deal_id, note_id=note_id)
        await service.session.commit()
        note_schema = present_note_item(note)
        return Response.success_response(message="Note deleted successfully", data=note_schema)
    except NotesServiceError as exc:
        await service.session.rollback()
        logger.warning("Failed to delete note: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error(
            "Unexpected error deleting note %s for deal %s: %s",
            note_id,
            deal_id,
            exc,
            exc_info=True,
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
