from typing import Any, TypeVar, cast

import fastapi
from pydantic import TypeAdapter
from src.api.schemas.base_models import GenericResponse

T = TypeVar("T")


class Response:
    @classmethod
    def _validate_data(cls, data: T | None, data_model: type[T] | None) -> T | None:
        """Validate data against a Pydantic model type if provided.

        Args:
            data: The data to validate
            data_model: The Pydantic model type to validate against (e.g., list[StakeholderDetail])

        Returns:
            Validated data ready for JSON response
        """
        if data_model is None or data is None:
            return data

        adapter = TypeAdapter(data_model)
        return cast(T, adapter.validate_python(data))

    @classmethod
    def _set_status_code(cls, response: fastapi.Response | None, status_code: int) -> None:
        if response is not None:
            response.status_code = status_code

    @classmethod
    def success_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
        status_code: int = fastapi.status.HTTP_200_OK,
        data_model: type[T] | None = None,
    ) -> GenericResponse[T]:
        """Create a success response with optional data validation.

        Args:
            message: Response message
            data: Response data
            data_model: Optional Pydantic model type for validation (e.g., list[StakeholderDetail])
                       When provided, data is validated against this type before serialization.

        Returns:
            GenericResponse with validated data
        """
        cls._set_status_code(response, status_code)
        validated_data = cls._validate_data(data, data_model)
        return GenericResponse(status="SUCCESS", message=message, data=validated_data)

    @classmethod
    def created_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
        data_model: type[T] | None = None,
    ) -> GenericResponse[T]:
        return cls.success_response(
            message=message,
            data=data,
            response=response,
            status_code=fastapi.status.HTTP_201_CREATED,
            data_model=data_model,
        )

    @classmethod
    def accepted_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
        data_model: type[T] | None = None,
    ) -> GenericResponse[T]:
        cls._set_status_code(response, fastapi.status.HTTP_202_ACCEPTED)
        validated_data = cls._validate_data(data, data_model)
        return GenericResponse(status="SUCCESS", message=message, data=validated_data)

    @classmethod
    def no_content_response(cls) -> fastapi.Response:
        return fastapi.Response(status_code=fastapi.status.HTTP_204_NO_CONTENT)

    @classmethod
    def internal_server_error_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
    ) -> GenericResponse[T]:
        return cls.error_response(
            status_code=fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=message,
            data=data,
            response=response,
        )

    @classmethod
    def bad_request_error_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
    ) -> GenericResponse[T]:
        return cls.error_response(
            status_code=fastapi.status.HTTP_400_BAD_REQUEST,
            message=message,
            data=data,
            response=response,
        )

    @classmethod
    def unauthorized_error_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
    ) -> GenericResponse[T]:
        return cls.error_response(
            status_code=fastapi.status.HTTP_401_UNAUTHORIZED,
            message=message,
            data=data,
            response=response,
        )

    @classmethod
    def forbidden_error_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
    ) -> GenericResponse[T]:
        return cls.error_response(
            status_code=fastapi.status.HTTP_403_FORBIDDEN,
            message=message,
            data=data,
            response=response,
        )

    @classmethod
    def not_found_error_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
    ) -> GenericResponse[T]:
        return cls.error_response(
            status_code=fastapi.status.HTTP_404_NOT_FOUND,
            message=message,
            data=data,
            response=response,
        )

    @classmethod
    def conflict_error_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
    ) -> GenericResponse[T]:
        return cls.error_response(
            status_code=fastapi.status.HTTP_409_CONFLICT,
            message=message,
            data=data,
            response=response,
        )

    @classmethod
    def request_entity_too_large_error_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
    ) -> GenericResponse[T]:
        return cls.error_response(
            status_code=fastapi.status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            message=message,
            data=data,
            response=response,
        )

    @classmethod
    def unsupported_media_type_error_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
    ) -> GenericResponse[T]:
        return cls.error_response(
            status_code=fastapi.status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            message=message,
            data=data,
            response=response,
        )

    @classmethod
    def unprocessable_entity_error_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
    ) -> GenericResponse[T]:
        return cls.error_response(
            status_code=fastapi.status.HTTP_422_UNPROCESSABLE_ENTITY,
            message=message,
            data=data,
            response=response,
        )

    @classmethod
    def too_many_requests_error_response(
        cls,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
    ) -> GenericResponse[T]:
        return cls.error_response(
            status_code=fastapi.status.HTTP_429_TOO_MANY_REQUESTS,
            message=message,
            data=data,
            response=response,
        )

    # Convenience generic error helper used by some legacy handlers expecting a
    # unified name.
    @classmethod
    def error_response(
        cls,
        status_code: int,
        message: Any,
        data: T | None = None,
        *,
        response: fastapi.Response | None = None,
        code: str | None = None,
    ) -> GenericResponse[T]:
        cls._set_status_code(response, status_code)
        return GenericResponse(status="ERROR", message=message, code=code, data=data)
