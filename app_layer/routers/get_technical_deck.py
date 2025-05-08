from uuid import UUID
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlmodel import Session, select

from core.dto.authentication import Authentication
from core.database import get_master_session
from app_layer.utils.logger import get_logger
from models.chat_db_models import ChatSession as ChatSessionDB
from models.chat_db_models import ChatMessage as ChatMessageDB
from models.http_response import Response

auth = Authentication()
logger = get_logger()
technical_deck = APIRouter()


@technical_deck.get("/get_technical_deck", response_model=Dict[str, Any])
def get_technical_deck(
    session_id: UUID = Query(..., description="Conversation thread ID"),
    message_id: UUID = Query(..., description="Unique message ID"),
    state: str = Query("ALL", description="state of the message"),
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Retrieve a specific chat session by its ID for the authenticated user/org,
    along with its messages as a list of links.
    """

    try:
        logger.info("Providing Deck Details to Frontend")

        # Get user id and organisation id of the user
        user_id = UUID(auth_data.get("user_id"))
        organisation_id = UUID(auth_data.get("organisation_id"))

        # Verify chat session
        logger.info("Querying for chat session: %s", session_id)
        statement = select(ChatSessionDB).where(
            ChatSessionDB.session_id == session_id,
            ChatSessionDB.user_id == user_id,
            ChatSessionDB.organisation_id == organisation_id,
        )
        chat = master_session.exec(statement).one_or_none()

        if not chat:
            logger.warning(f"Chat session not found: session_id={session_id}, user_id={user_id}")
            return Response.not_found_error_response("Chat Session does not exists")

        # Fetch the messages associated with the chat session
        logger.info(f"Querying for messages for chat session: {session_id} and message: {message_id}")
        message_statement = select(ChatMessageDB).where(
            ChatMessageDB.session_id == session_id, ChatMessageDB.message_id == message_id
        )
        message = master_session.exec(message_statement).first()
        tech_deck_details = message.technical_deck_details or {}

        if not tech_deck_details:
            logger.warning(f"Technical Deck not found: session_id={session_id}, message_id={message_id}")
            return Response.not_found_error_response("Technical Deck not found")

        match state:
            case "ALL":
                return Response.success_response("Complete Technical Deck Details", tech_deck_details)
            case _:
                return Response.success_response("Complete Technical Deck Details", tech_deck_details)

    except Exception as e:
        logger.error("Error occurred: %s", str(e))
        return Response.internal_server_error_response(f"Error occurred {str(e)}")
