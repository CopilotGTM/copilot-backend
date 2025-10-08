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
            content=jsonable_encoder(
                content, custom_encoder={datetime: custom_datetime_encoder}
            ),
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
    def no_content_response(cls):
        return fastapi.Response(status_code=fastapi.status.HTTP_204_NO_CONTENT)

    @classmethod
    def internal_server_error_response(cls, message: Any, data: Any = None):
        return cls._response(
            fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR, "ERROR", message, data
        )

    @classmethod
    def bad_request_error_response(cls, message: Any, data: Any = None):
        return cls._response(
            fastapi.status.HTTP_400_BAD_REQUEST, "ERROR", message, data
        )

    @classmethod
    def unauthorized_error_response(cls, message: Any, data: Any = None):
        return cls._response(
            fastapi.status.HTTP_401_UNAUTHORIZED, "ERROR", message, data
        )

    @classmethod
    def forbidden_error_response(cls, message: Any, data: Any = None):
        return cls._response(fastapi.status.HTTP_403_FORBIDDEN, "ERROR", message, data)

    @classmethod
    def not_found_error_response(cls, message: Any, data: Any = None):
        return cls._response(fastapi.status.HTTP_404_NOT_FOUND, "ERROR", message, data)

    @classmethod
    def conflict_error_response(cls, message: Any):
        return cls._response(fastapi.status.HTTP_409_CONFLICT, "ERROR", message)

    @classmethod
    def request_entity_too_large_error_response(cls, message: Any, data: Any = None):
        return cls._response(
            fastapi.status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "ERROR", message, data
        )

    @classmethod
    def unsupported_media_type_error_response(cls, message: Any, data: Any = None):
        return cls._response(
            fastapi.status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "ERROR", message, data
        )

    @classmethod
    def unprocessable_entity_error_response(cls, message: Any, data: Any = None):
        return cls._response(
            fastapi.status.HTTP_422_UNPROCESSABLE_ENTITY, "ERROR", message, data
        )

    @classmethod
    def too_many_requests_error_response(cls, message: Any, data: Any = None):
        return cls._response(
            fastapi.status.HTTP_429_TOO_MANY_REQUESTS, "ERROR", message, data
        )

    # Convenience generic error helper used by some legacy handlers expecting a unified name.
    @classmethod
    def error_response(cls, status_code: int, message: Any, data: Any = None):
        return cls._response(status_code, "ERROR", message, data)
