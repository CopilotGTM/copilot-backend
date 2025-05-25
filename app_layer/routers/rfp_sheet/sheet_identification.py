from uuid import UUID
from typing import Any, Dict, List, Tuple
import os
import requests
import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from urllib.parse import urlparse, unquote, ParseResult
from io import BytesIO
import openpyxl
from openpyxl import load_workbook
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from datetime import datetime

from core.dto.authentication import Authentication
from core.database import get_master_session
from app_layer.utils.logger import get_logger
from models.chat_models import UploadFileURL
from core.app_state import fastapp
from workflows.rfp_sheet_mapping.sheet_mapping_workflow import SheetIdentificationWorkflow

from models.chat_socket_models import ChatMessageReceived, AttachmentModel
from models.chat_db_models import ChatSession, ChatMessage
from models.http_response import Response

auth = Authentication()
logger = get_logger()
rfp__sheet_router = APIRouter()


def get_s3_client():
    """Create and return an S3 client"""
    return boto3.client("s3", region_name="us-east-1")


def parse_s3_url(url: str) -> Tuple[str, str]:
    """
    Parse S3 URL into bucket name and key using standard URL parsing

    Args:
        url: The S3 URL to parse (can be string or HttpUrl)

    Returns:
        tuple: (bucket_name, key)

    Example:
        >>> parse_s3_url("https://my-bucket.s3.amazonaws.com/path/to/file.xlsx")
        ('my-bucket', 'path/to/file.xlsx')
    """
    try:
        # Convert HttpUrl to string if needed
        url_str = str(url)

        # Parse the URL into components
        parsed: ParseResult = urlparse(url_str)

        if not parsed.netloc:
            raise ValueError("Invalid URL: missing hostname")

        # Extract bucket name from hostname (remove .s3.amazonaws.com)
        bucket_name = parsed.netloc.split(".")[0]

        if not bucket_name:
            raise ValueError("Invalid S3 URL: could not extract bucket name")

        # Get the path and remove leading slash
        # First replace + with %20, then URL decode
        path = parsed.path.lstrip("/")
        path = path.replace("+", "%20")  # Replace + with %20 before decoding
        key = unquote(path)

        if not key:
            raise ValueError("Invalid S3 URL: empty key")

        logger.info(f"Successfully parsed S3 URL - Bucket: {bucket_name}, Key: {key}")
        return bucket_name, key

    except Exception as e:
        logger.error(f"Error parsing S3 URL '{url}': {str(e)}")
        raise ValueError(f"Invalid S3 URL format: {url}")


def read_excel_from_url(url: str) -> Dict[str, Any]:
    """
    Read Excel file from S3 URL and return sheet information using openpyxl

    Args:
        url: The S3 URL of the Excel file

    Returns:
        Dict[str, Any]: Dictionary with sheet information
    """
    try:
        # Parse the S3 URL
        bucket_name, key = parse_s3_url(url)
        logger.info(f"Successfully parsed S3 URL - Bucket: {bucket_name}, Key: {key}")

        # Get S3 client
        s3 = get_s3_client()

        # Download file to memory
        excel_obj = BytesIO()
        s3.download_fileobj(Bucket=bucket_name, Key=key, Fileobj=excel_obj)
        excel_obj.seek(0)
        logger.info("Successfully downloaded Excel file from S3")

        # Load workbook
        workbook = load_workbook(excel_obj, read_only=True, data_only=True)
        sheets_data = {}

        for sheet_name in workbook.sheetnames:
            logger.info(f"Processing sheet: {sheet_name}")
            sheet = workbook[sheet_name]

            # Get data for up to 50 rows
            rows_data = []
            for i, row in enumerate(sheet.rows):
                if i > 50:  # Get up to 50 rows
                    break
                row_data = [cell.value for cell in row]
                rows_data.append(row_data)

            sheets_data[sheet_name] = rows_data
            logger.info(f"Completed processing sheet {sheet_name}")

        workbook.close()
        logger.info(f"Successfully processed all sheets. Total sheets: {len(sheets_data)}")
        return sheets_data

    except Exception as e:
        logger.error(f"Error reading Excel file from URL: {e}")
        raise


@rfp__sheet_router.post("/sheet_identification", response_model=UploadFileURL)
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
        master_session.add(assistant_msg)
        master_session.commit()
        master_session.refresh(assistant_msg)
        logger.info(f"Assistant Message Stored {assistant_msg}")

        # Read the Excel file from S3 URL
        file_url = input_file[0].url
        sheets_data = read_excel_from_url(file_url)

        # Initialize the workflow
        workflow = SheetIdentificationWorkflow(websocket_room=str(session_id), system_message_id=system_message_id)

        # Process sheets in parallel using asyncio
        workflow = SheetIdentificationWorkflow()
        sheets_details = []

        for sheet_name, sheet_info in sheets_data.items():
            slide_output = await workflow.run_workflow({"sheet_name": sheet_name, "rows": sheet_info})
            sheets_details.append(slide_output)

        sheet_list_details = [
            {
                "sheet_name": sheet.sheet_name,
                "columns": [
                    {"name": column.name, "is_question_column": False, "is_answer_column": False}
                    for column in sheet.columns
                ],
            }
            for sheet in sheets_details
        ]

        # Update assistant message with sheet details
        assistant_msg.rfp_details = {"sheets_details": sheet_list_details, "state": "SHEET_IDENTIFICATION"}
        master_session.add(assistant_msg)
        master_session.commit()
        master_session.refresh(assistant_msg)

        return Response.success_response(
            message="Sheet analysis completed successfully", data={"sheets_details": sheet_list_details}
        )

    except ValueError as e:
        logger.error(f"Invalid input: {e}")
        return Response.bad_request_error_response(str(e))
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        return Response.internal_server_error_response(message="Failed to process RFP sheet", data=None)
