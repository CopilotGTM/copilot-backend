from uuid import UUID
from typing import Any, Dict, List
import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from core.dto.authentication import Authentication
from core.database import get_master_session
from app_layer.utils.logger import get_logger
from models.chat_models import UploadFileURL
from core.app_state import fastapp
from infrastructure.generate_presigned_url import GeneratePresignedURL

auth = Authentication()
logger = get_logger()
upload_router = APIRouter()

# Define constants for file validation
MAX_FILE_SIZE_MB = 50  # Maximum file size in MB
ALLOWED_FILE_TYPES = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/json": ".json",
    "text/csv": ".csv",
}
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024  # Convert MB to bytes


@upload_router.post("/get_upload_url")
def get_upload_url(
    file_info: UploadFileURL,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
) -> Dict[str, str]:
    """
    Generate a presigned URL for file upload.
    """
    try:
        file_name = file_info.file_name
        file_type = file_info.file_type
        file_size = file_info.file_size
        chat_id = file_info.session_id

        # Validate file size
        if file_size > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File size exceeds maximum limit of {MAX_FILE_SIZE_MB}MB",
            )

        # Validate file type
        if file_type not in ALLOWED_FILE_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported file type. Allowed types: {', '.join(ALLOWED_FILE_TYPES.values())}",
            )

        # Validate file extension matches content type
        file_extension = os.path.splitext(file_name)[1].lower()
        expected_extension = ALLOWED_FILE_TYPES[file_type]
        if file_extension != expected_extension.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File extension does not match the content type. Expected {expected_extension} for {file_type}",
            )

        # Extract and validate user/org IDs from the decoded JWT payload
        try:
            user_id = UUID(auth_data.get("user_id"))
            organisation_id = UUID(auth_data.get("organisation_id"))
        except (ValueError, TypeError):
            logger.error("Invalid UUID in auth payload: %s", auth_data)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid authentication payload",
            )

        # Generate the upload URL
        logger.info(f"Requesting upload URL for file: {file_name}, type: {file_type}, size: {file_size}")
        file_path = f"uploads/{organisation_id}/{user_id}/{chat_id}/{file_name}"

        url_generator = GeneratePresignedURL()
        presigned_url = url_generator.generate_presigned_url(
            cloud_name=fastapp.state.CLOUD_PROVIDER.lower(),
            container_name=fastapp.state.APPLICATION_STORAGE_NAME,
            blob_path=file_path,
            expire_seconds=3600,
        )

        if not presigned_url:
            logger.error(f"Failed to generate presigned URL for file: {file_name}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate upload URL. Please check AWS credentials and configuration.",
            )

        logger.info(f"Successfully generated presigned URL for file: {file_name}")
        return {"upload_url": presigned_url}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in get_upload_url: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while generating the upload URL",
        )
