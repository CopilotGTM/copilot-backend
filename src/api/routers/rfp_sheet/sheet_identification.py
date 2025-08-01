from uuid import UUID
from typing import Any, Dict, List, Tuple
from fastapi import APIRouter, Depends
from sqlmodel import Session
from openpyxl import load_workbook
import uuid
from datetime import datetime


from src.domain.models.authentication import Authentication
from src.infra.utils.database import get_master_session
from src.infra.utils.logger import get_logger
from src.infra.utils.app_state import fastapp
from src.infra.storage.get_cloud_uri import CloudURIConverter, CloudConfig
from src.infra.storage.cloud_file_reader import get_cloud_reader

from src.api.schemas.chat_socket_models import ChatMessageReceived, AttachmentModel
from src.domain.models.organisation_tables import ChatMessage
from src.api.utils.http_response import Response

from src.domain.workflows.rfp_sheet_utils.unmerge_data import get_unmerged_cell_data
from src.domain.workflows.rfp_sheet_utils.sheet_processor import process_sheets_in_parallel
from src.api.schemas.sheet_analysis_response import SheetAnalysisResponse

auth = Authentication()
logger = get_logger()
rfp__sheet_router = APIRouter()


@rfp__sheet_router.post("/sheet_identification", response_model=SheetAnalysisResponse)
async def sheet_list(
    data: ChatMessageReceived,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, str]:
    """Process RFP sheet and return sheet details"""

    try:

        # Extract and validate user/org IDs from the decoded payload
        user_id = UUID(auth_data.get("user_id"))
        organisation_id = UUID(auth_data.get("organisation_id"))

        # Extract attachments from the request data and validate that a file was provided
        input_file: List[AttachmentModel] = data.attachments
        if not input_file:
            logger.error("No file provided in the request")
            return Response.bad_request_error_response("Sheet attachment not found.")

        # Verify there should only be one file
        if len(input_file) > 1:
            logger.error("Multiple files provided in the request")
            return Response.bad_request_error_response("Only one file is allowed")

        # Verify input sheet type
        file_type = input_file[0].mimetype
        if file_type not in ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/csv"]:
            logger.error("Unsupported file type: %s", file_type)
            return Response.bad_request_error_response("Only Excel (.xlsx) or CSV files are supported")

        # Extract fields from your payload
        session_id = data.session_id

        # Convert attachments to dict with serialized datetime
        attachments_data = [
            {**attachment.model_dump(), "url": str(attachment.url), "uploaded_at": attachment.uploaded_at.isoformat()}
            for attachment in data.attachments
        ]

        # Save user message in DB
        user_msg = ChatMessage(
            session_id=session_id,
            message_id=data.message_id,
            role=data.role,
            content=data.content or "",
            attachments=attachments_data,
            message_type="RFP_SHEET",
            timestamp=data.timestamp.isoformat() if isinstance(data.timestamp, datetime) else data.timestamp,
        )
        master_session.add(user_msg)
        master_session.commit()
        master_session.refresh(user_msg)
        logger.info(f"User Message Stored - {user_msg}")

        # Save Assistant Message in DB
        system_message_id = str(uuid.uuid4())
        assistant_msg = ChatMessage(
            session_id=session_id,
            message_id=system_message_id,
            role="assistant",
            content="RFP Sheet Answers",
            attachments=attachments_data,
            message_type="RFP_SHEET",
            timestamp=datetime.utcnow().isoformat(),
        )
        # cloud_config = CloudConfig("aws", "us-east-1")

        # Read the Excel file from S3 URL
        cloud_config = CloudConfig(fastapp.state.CLOUD_PROVIDER, fastapp.state.APPLICATION_REGION)
        file_url = str(input_file[0].url)
        storage_uri = CloudURIConverter(cloud_config).convert_url(file_url)
        sheet_object = get_cloud_reader(cloud_config).read_file_to_memory(storage_uri)
        workbook = load_workbook(sheet_object, data_only=True)
        sheet_wise_data = get_unmerged_cell_data(workbook)

        # Process sheets in parallel using the utility function
        sheet_list_details = await process_sheets_in_parallel(
            sheets_data=sheet_wise_data, session_id=str(session_id), system_message_id=system_message_id
        )

        # Update assistant message with sheet details
        assistant_msg.rfp_details = {"sheets_details": sheet_list_details, "state": "SHEET_IDENTIFICATION"}
        master_session.add(assistant_msg)
        master_session.commit()
        master_session.refresh(assistant_msg)
        logger.info(f"Assistant Message Stored")

        return Response.success_response(
            message="Sheet analysis completed successfully",
            data={"sheets_details": sheet_list_details, "message_id": system_message_id},
        )

    except ValueError as e:
        logger.error(f"Invalid input: {e}")
        return Response.bad_request_error_response(str(e))
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        return Response.internal_server_error_response(message="Failed to process RFP sheet", data=None)
