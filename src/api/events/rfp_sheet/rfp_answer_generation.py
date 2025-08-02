# app_layer/websockets/transcript_summary_handler.py
from ...events.socket_app import sio
from src.infra.utils.logger import get_logger
from socketio.exceptions import ConnectionRefusedError
import json
from typing import Dict, List
from openpyxl import load_workbook

from sqlmodel import select
from src.domain.models.organisation_tables import ChatMessage
from src.infra.utils.database import get_master_session
from src.infra.messaging.redis_client import get_socket_connection
# from src.api.schemas.chat_socket_models import AttachmentModel
from src.infra.utils.app_state import fastapp
from src.api.utils.websocket_response import WebSocketResponse

from src.service.technical_deck.transcript_summary.transcript_workflow import TranscriptSummaryAgent
from src.api.schemas.sheet_analysis_response import SheetSelectionInput

from src.domain.workflows.rfp_sheet_utils.unmerge_sheet_data import get_unmerged_sheet_data
from src.domain.workflows.rfp_sheet_utils.sheet_processor import process_sheets_in_parallel
from src.domain.workflows.rfp_sheet_utils.row_processor import create_row_object
from src.domain.workflows.rfp_sheet_utils.answer_processor import process_answers_in_parallel
from src.infra.storage.get_cloud_uri import CloudURIConverter, CloudConfig
from src.infra.storage.cloud_file_reader import get_cloud_reader

logger = get_logger()

WEBSOCKET_NAMESPACE = "/websocket/v1"
RESPONSE_EVENT = "recieve_sheet_answers"


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def send_sheet_selections(
    sid,
    data: SheetSelectionInput,
):
    """
    1. Validate the incoming payload
    2. Persist the user ChatMessage (+ transcript in JSONB)
    3. Stream the agent response
    4. Persist the assistant ChatMessage (+ transcript_summary in JSONB)
    """
    master_session = None
    system_message_id = None
    session_id = None

    try:
        logger.info(f"Transcript Summary WebSocket message received: sid={sid}")

        # Auth / connection details
        connection_details = await get_socket_connection(sid)
        if not connection_details:
            logger.warning(f"Connection details not found for sid: {sid}")
            raise ConnectionRefusedError("Unauthorized")

        # Extract user_id and organisation_id from the connection details
        connection_details = json.loads(connection_details)
        user_id = connection_details.get("user_id")
        organisation_id = connection_details.get("organisation_id")
        if not user_id or not organisation_id:
            logger.error("Connect refused: token missing user_id/organisation_id")
            raise ConnectionRefusedError("Invalid authentication payload")

        # Extract fields from your payload
        session_id = data.get("session_id")
        system_message_id = data.get("system_message_id")
        sheet_details = data.get("sheets_details")
        master_session = next(get_master_session())

        # Read the Message from chat message using chat_id
        logger.info(f"Fetching chat message for session_id: {session_id}")
        existing_rfp_details: ChatMessage = master_session.exec(
            select(ChatMessage).where(
                (ChatMessage.session_id == session_id) & (ChatMessage.message_id == system_message_id)
            )
        ).first()

        if not existing_rfp_details:
            logger.error(f"No RFP details found for session_id={session_id}")
            await WebSocketResponse.emit_error(
                room=sid,
                event=RESPONSE_EVENT,
                chat_id=session_id,
                message_id=system_message_id,
                error_message="No RFP details found",
                message_type="RFP_SHEET",
            )
            return

        # Processing to Generate Response
        cloud_config = CloudConfig(fastapp.state.CLOUD_PROVIDER, fastapp.state.APPLICATION_REGION)
        input_file = existing_rfp_details.attachments
        file_url = str(input_file[0].get("url"))
        storage_uri = CloudURIConverter(cloud_config).convert_url(file_url)
        sheet_object = get_cloud_reader(cloud_config).read_file_to_memory(storage_uri)
        workbook = load_workbook(sheet_object, data_only=True)

        existing_sheet_details = existing_rfp_details.rfp_details.get("sheets_details", [])

        # Update is_sheet_selected based on new selections
        for existing_sheet in existing_sheet_details:
            # Find matching sheet in new selections
            matching_sheet = next(
                (sheet for sheet in sheet_details if sheet.get("sheet_name") == existing_sheet.get("sheet_name")), None
            )
            if matching_sheet:
                existing_sheet["is_sheet_selected"] = True
                # Update column flags based on question and answer columns
                for column in existing_sheet.get("columns", []):
                    column["is_question_column"] = column["name"] == matching_sheet.get("question_column")
                    column["is_answer_column"] = column["name"] == matching_sheet.get("answer_column")

        sheet_selected = [i for i in existing_sheet_details if i.get("is_sheet_selected")]
        for selected in sheet_selected:
            # Get column configurations
            columns = selected.get("columns", [])
            column_row_number = selected.get("column_row_number", 1)

            # Extract question and answer columns
            question_columns = [col["name"] for col in columns if col.get("is_question_column", False)]
            answer_columns = [col["name"] for col in columns if col.get("is_answer_column", False)]

            # Validate that only one column is selected for each type
            if len(question_columns) > 1:
                logger.error(
                    f"Multiple question columns selected in sheet {selected.get('sheet_name')}. Only one question column is allowed."
                )
                await WebSocketResponse.emit_error(
                    room=sid,
                    event=RESPONSE_EVENT,
                    chat_id=session_id,
                    message_id=system_message_id,
                    error_message=f"Multiple question columns selected in sheet {selected.get('sheet_name')}. Only one question column is allowed.",
                    message_type="RFP_SHEET",
                )
                continue

            if len(answer_columns) > 1:
                logger.error(
                    f"Multiple answer columns selected in sheet {selected.get('sheet_name')}. Only one answer column is allowed."
                )
                await WebSocketResponse.emit_error(
                    room=sid,
                    event=RESPONSE_EVENT,
                    chat_id=session_id,
                    message_id=system_message_id,
                    error_message=f"Multiple answer columns selected in sheet {selected.get('sheet_name')}. Only one answer column is allowed.",
                    message_type="RFP_SHEET",
                )
                continue

            # Get the single selected column for each type (if any)
            question_column = question_columns[0] if question_columns else None
            answer_column = answer_columns[0] if answer_columns else None

            data = get_unmerged_sheet_data(workbook, selected.get("sheet_name", ""))
            if not data:
                logger.warning(f"No data found for sheet: {selected.get('sheet_name')}")
                await WebSocketResponse.emit_error(
                    room=sid,
                    event=RESPONSE_EVENT,
                    chat_id=session_id,
                    message_id=system_message_id,
                    error_message=f"No data found for sheet: {selected.get('sheet_name')}",
                    message_type="RFP_SHEET",
                )
                continue

            # Process each row in the sheet data
            row_objects = []
            for row_index, row_data in enumerate(data.get("data", []), start=2):
                row_object = create_row_object(
                    sheet_name=data.get("sheetname"),
                    row_data=row_data,
                    row_index=row_index,
                    question_column=question_column,
                    answer_column=answer_column,
                )
                if row_object:
                    # Add answer column to row object
                    row_object["answer_column"] = answer_column
                    row_objects.append(row_object)

            # Process answers for all row objects in parallel
            if row_objects:
                logger.info(f"length of row objects{len(row_objects)}")
                processed_answers = await process_answers_in_parallel(
                    row_objects=row_objects, session_id=session_id, system_message_id=system_message_id
                )
                # Emit chunk for each processed answer
                for answer in processed_answers:
                    await WebSocketResponse.emit_chunk(
                        room=sid,
                        event=RESPONSE_EVENT,
                        chat_id=session_id,
                        message_id=system_message_id,
                        response=answer,
                        message_type="RFP_SHEET",
                    )

    except Exception as e:
        logger.exception("Error in send_sheet_selections")
        if system_message_id and session_id:
            await WebSocketResponse.emit_error(
                room=sid,
                event=RESPONSE_EVENT,
                chat_id=session_id,
                message_id=system_message_id,
                error_message="Failed to process sheet selections. Please try again later.",
                message_type="RFP_SHEET",
            )

    finally:
        if system_message_id and session_id:
            await WebSocketResponse.emit_completion(
                room=sid,
                event=RESPONSE_EVENT,
                chat_id=session_id,
                message_id=system_message_id,
                message_type="RFP_SHEET",
            )
        # Clean up DB session generator if we opened one
        try:
            if master_session is not None:
                master_session.close()
        except Exception:
            pass
