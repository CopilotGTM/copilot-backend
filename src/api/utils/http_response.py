from datetime import datetime
from typing import Any

import fastapi
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


class Response:
    @classmethod
    def _response(
        cls,
        status_code: fastapi.status,
        status: str,
        message: str = None,
        data: Any = None,
    ):
        content = {"status": status, "data": data, "message": message}
        custom_datetime_encoder = lambda obj: obj.isoformat() + "Z"
        return JSONResponse(
            status_code=status_code,
            content=jsonable_encoder(content, custom_encoder={datetime: custom_datetime_encoder}),
        )

    @classmethod
    def success_response(cls, message: Any, data: Any = None):
        return cls._response(fastapi.status.HTTP_200_OK, "SUCCESS", message, data)

    @classmethod
    def created_response(cls, message: Any):
        return cls._response(fastapi.status.HTTP_201_CREATED, "SUCCESS", message)

    @classmethod
    def accepted_response(cls, message: Any, data: Any = None):
        return cls._response(fastapi.status.HTTP_202_ACCEPTED, "SUCCESS", message, data)

    @classmethod
    def internal_server_error_response(cls, message: Any, data: Any):
        return cls._response(fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR, "ERROR", message, data)

    @classmethod
    def bad_request_error_response(cls, message: Any, data: Any = None):
        return cls._response(fastapi.status.HTTP_400_BAD_REQUEST, "ERROR", message, data)

    @classmethod
    def invalid_authentication_request_error_response(cls, message: Any, data: Any = None):
        return cls._response(fastapi.status.HTTP_400_BAD_REQUEST, "ERROR", message, data)

    @classmethod
    def forbidden_error_response(cls, message: Any, data: Any = None):
        return cls._response(fastapi.status.HTTP_403_FORBIDDEN, "ERROR", message, data)

    @classmethod
    def not_found_error_response(cls, message: Any, data: Any = None):
        return cls._response(fastapi.status.HTTP_404_NOT_FOUND, "ERROR", message, data)

    @classmethod
    def conflict_error_response(cls, message: Any):
        return cls._response(fastapi.status.HTTP_409_CONFLICT, "ERROR", message)
