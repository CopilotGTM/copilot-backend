from uuid import UUID
from typing import Any, Dict, List
import os
import requests

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from src.domain.models.authentication import Authentication
from src.infra.utils.database import get_master_session
from src.infra.utils.logger import get_logger
from src.api.schemas.chat_models import UploadFileURL
from src.infra.utils.app_state import fastapp

from src.api.schemas.chat_socket_models import ChatMessageReceived, AttachmentModel
from src.domain.models.organisation_tables import ChatSession, ChatMessage

from io import BytesIO
import pandas as pd
import uuid

auth = Authentication()
logger = get_logger()
rfp_router = APIRouter()


@rfp_router.post("/sheet_identification", response_model=UploadFileURL)
def sheet_list(
    data: ChatMessageReceived,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
) -> Dict[str, str]:
    """ """

    try:
        input_file: List[AttachmentModel] = data.get("attachments")
        if not input_file:
            logger.error("No file provided in the request")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No file provided",
            )

        # Verify there should only be one file
        if len(input_file) > 1:
            logger.error("Multiple files provided in the request")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only one file is allowed",
            )

        # Verify if only csv or xlsx file is provided from file type
        file_type = input_file[0].mimetype
        if file_type not in ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/csv"]:
            logger.error("Unsupported file type: %s", file_type)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported file type",
            )

        # Extract fields from your payload
        chat_id = data.get("session_id")
        session_gen = get_master_session()
        master_session = next(session_gen)
        chat_message = ChatMessage(
            session_id=chat_id,
            message_id=data.get("message_id"),
            role="user",
            content=data.get("content", ""),
            attachments=data.get("attachments"),
            message_type=data.get("message_type", "RFP_ANSWERS"),
        )
        master_session.add(chat_message)
        master_session.commit()
        master_session.refresh(chat_message)

        # Read the file using url in such a way that you identify the file type
        file_url = input_file[0].url

        # 1. Download into memory
        try:
            resp = requests.get(file_url, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Failed to download file from %s: %s", file_url, e)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not fetch the uploaded file",
            )

        # 5. Read sheet names & headers via pandas
        sheets: List[Dict[str, Any]] = []
        try:
            if file_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
                # Load sheet names without downloading full data
                xls = pd.ExcelFile(file_url)
                for name in xls.sheet_names:
                    # Read only the header row
                    df = pd.read_excel(
                        file_url,
                        sheet_name=name,
                        nrows=0,
                    )
                    sheets.append(
                        {
                            "sheetName": name,
                            "columns": [
                                {"name": col, "isQuestionColumn": False, "isAnswerColumn": False} for col in df.columns
                            ],
                        }
                    )

            else:  # CSV
                df = pd.read_csv(
                    file_url,
                    nrows=0,
                )
                sheets.append(
                    {
                        "sheetName": "csv",
                        "columns": [
                            {"name": col, "isQuestionColumn": False, "isAnswerColumn": False} for col in df.columns
                        ],
                    }
                )

        except Exception as e:
            logger.error("Error reading %s via pandas: %s", file_url, e)
            raise HTTPException(
                status_code=400,
                detail="Unable to parse the spreadsheet or CSV file",
            )
        assistant_msg = ChatMessage(
            session_id=chat_id,
            message_id=str(uuid.uuid4()),
            role="assistant",
            content="",
            attachments=None,
            message_type="RFP_ANSWERS",
            rfp_details={
                "sheets_details": sheets,
            },
        )
        master_session.add(assistant_msg)
        master_session.commit()
        master_session.refresh(assistant_msg)
        logger.info(f"Sheet details stored in DB: {sheets}")
        return {"sheets_details": sheets}

    except Exception as e:
        logger.error(f"Error processing request: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
