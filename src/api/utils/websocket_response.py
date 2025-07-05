from typing import Any, Optional, Dict, List
from socketio import AsyncServer
# from src.api.schemas.chat_socket_models import ChatMessageReceived
from src.api.events.socket_app import sio as InputSio


class WebSocketResponse:
    sio = InputSio
    namespace = "/websocket/v1"

    @classmethod
    async def emit_response(
        cls,
        room: str,
        event: str,
        chat_id: str,
        message_id: str,
        response: any = "",
        is_complete: bool = False,
        is_line_complete: bool = False,
        is_error: bool = False,
        error_response: Optional[str] = None,
        is_first_message: bool = False,
        message_type: str = "GENERAL_CHAT",
        additional_data: Optional[Dict[str, Any]] = None,
    ):
        """
        Emit a WebSocket response with standardized structure

        Args:
            room: The room to emit to
            event: The event name to emit to
            chat_id: The chat session ID
            message_id: The message ID
            response: The response content
            is_complete: Whether this is the final message
            is_line_complete: Whether the current line is complete
            is_error: Whether this is an error message
            error_response: Error message if is_error is True
            is_first_message: Whether this is the first message
            message_type: Type of message (e.g., GENERAL_CHAT, TECHNICAL_DECK)
            additional_data: Any additional data to include in the response
        """
        response_data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "isComplete": is_complete,
            "isLineComplete": is_line_complete,
            "isError": is_error,
            "response": response,
            "isFirstMessage": is_first_message,
            "message_type": message_type,
        }

        if error_response:
            response_data["errorResponse"] = error_response

        if additional_data:
            response_data.update(additional_data)

        await cls.sio.emit(event, response_data, room=room, namespace=cls.namespace)

    @classmethod
    async def emit_first_message(
        cls,
        room: str,
        event: str,
        chat_id: str,
        message_id: str,
        message_type: str = "GENERAL_CHAT",
        response: any = "",
        additional_data: Optional[Dict[str, Any]] = None,
    ):
        """
        Emit the first message to share message ID with the client

        Args:
            room: The room to emit to
            event: The event name to emit to
            chat_id: The chat session ID
            message_id: The message ID
            message_type: Type of message
            additional_data: Any additional data to include
        """
        await cls.emit_response(
            room=room,
            event=event,
            chat_id=chat_id,
            message_id=message_id,
            is_first_message=True,
            message_type=message_type,
            response=response,
            additional_data=additional_data,
        )

    @classmethod
    async def emit_chunk(
        cls,
        room: str,
        event: str,
        chat_id: str,
        message_id: str,
        response: any,
        is_complete: bool = False,
        is_line_complete: bool = False,
        message_type: str = "GENERAL_CHAT",
        additional_data: Optional[Dict[str, Any]] = None,
    ):
        """
        Emit a response chunk

        Args:
            room: The room to emit to
            event: The event name to emit to
            chat_id: The chat session ID
            message_id: The message ID
            response: The response content
            is_complete: Whether this is the final message
            is_line_complete: Whether the current line is complete
            message_type: Type of message
            additional_data: Any additional data to include
        """
        await cls.emit_response(
            room=room,
            event=event,
            chat_id=chat_id,
            message_id=message_id,
            response=response,
            is_complete=is_complete,
            is_line_complete=is_line_complete,
            message_type=message_type,
            additional_data=additional_data,
        )

    @classmethod
    async def emit_error(
        cls,
        room: str,
        event: str,
        chat_id: str,
        message_id: str,
        error_message: str,
        is_complete: bool = True,
        message_type: str = "GENERAL_CHAT",
        additional_data: Optional[Dict[str, Any]] = None,
    ):
        """
        Emit an error response

        Args:
            room: The room to emit to
            event: The event name to emit to
            chat_id: The chat session ID
            message_id: The message ID
            error_message: The error message
            is_complete: Whether this is the final message
            message_type: Type of message
            additional_data: Any additional data to include
        """
        await cls.emit_response(
            room=room,
            event=event,
            chat_id=chat_id,
            message_id=message_id,
            is_error=True,
            error_response=error_message,
            is_complete=is_complete,
            message_type=message_type,
            additional_data=additional_data,
        )

    @classmethod
    async def emit_completion(
        cls,
        room: str,
        event: str,
        chat_id: str,
        message_id: str,
        message_type: str = "GENERAL_CHAT",
        additional_data: Optional[Dict[str, Any]] = None,
    ):
        """
        Emit a completion message

        Args:
            room: The room to emit to
            event: The event name to emit to
            chat_id: The chat session ID
            message_id: The message ID
            message_type: Type of message
            additional_data: Any additional data to include
        """
        await cls.emit_response(
            room=room,
            event=event,
            chat_id=chat_id,
            message_id=message_id,
            is_complete=True,
            message_type=message_type,
            additional_data=additional_data,
        )
