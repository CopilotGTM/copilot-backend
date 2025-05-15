from uuid import UUID
from typing import Any, Dict
from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from core.dto.authentication import Authentication
from core.database import get_master_session
from app_layer.utils.logger import get_logger

from models.chat_db_models import ChatSession as ChatSessionDB
from models.chat_db_models import ChatMessage as ChatMessageDB
from models.http_response import Response
from models.technical_deck_models import TemplateSelection

auth = Authentication()
logger = get_logger()
select_template = APIRouter()


@select_template.post("/select_template")
async def select_template_handler(
    template_selection: TemplateSelection,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
):
    """
    Handle the selection of a template for a technical deck.
    This endpoint updates the technical deck details with the selected template information for a given chat session.
    Args:
        template_selection (TemplateSelection): The template selection details including session_id, message_id and template details
        auth_data (Dict[str, Any]): Authentication data containing user and organization IDs from JWT
        master_session (Session): Database session for master database operations
    Returns:
        JSON: Success response with message on successful template selection
            Error response with appropriate message on failure
    Raises:
        Exception: If there's any error during the template selection process
    Flow:
        1. Validates user authentication and extracts user/org IDs
        2. Verifies chat session exists for the given user/org
        3. Fetches the deck message to be updated
        4. Updates technical deck details with selected template
        5. Commits changes to database
    Example Response:
        Success: {"status": "success", "message": "Template selected successfully", "data": null}
        Error: {"status": "error", "message": "Chat session not found", "data": null}
    """
    try:
        # Extract and validate user/org IDs from the decoded JWT payload
        user_id = UUID(auth_data.get("user_id"))
        organisation_id = UUID(auth_data.get("organisation_id"))

        # Fetch the chat session to verify
        statement = select(ChatSessionDB).where(
            ChatSessionDB.session_id == template_selection.session_id,
            ChatSessionDB.user_id == user_id,
            ChatSessionDB.organisation_id == organisation_id,
            ChatSessionDB.is_deleted == False,
        )
        chat = master_session.exec(statement).one_or_none()

        if not chat:
            logger.error(f"Chat session not found for user/org: {user_id}/{organisation_id}")
            return Response.not_found_error_response(message="Chat session not found")

        # Fetch deck message to update
        statement = select(ChatMessageDB).where(
            ChatMessageDB.message_id == template_selection.message_id,
            ChatMessageDB.session_id == template_selection.session_id,
        )
        deck_message = master_session.exec(statement).one_or_none()

        if not deck_message:
            logger.error(f"Deck message not found for user/org: {user_id}/{organisation_id}")
            return Response.not_found_error_response(message="Technical Deck not found")

        # Update the deck message with the selected template
        technical_deck_details = deck_message.technical_deck_details
        technical_deck_details["selected_template_details"] = template_selection.template_details

        # Update the deck message
        deck_message.technical_deck_details = technical_deck_details

        # Commit the changes
        master_session.add(deck_message)
        master_session.commit()
        master_session.refresh(deck_message)
        logger.info("Template selected successfully and deck message updated")

        return Response.success_response("Template selected successfully", None)
    except Exception as error:
        logger.error(f"Error selecting template: {error}")
        return Response.internal_server_error_response("Unable to process request", None)
