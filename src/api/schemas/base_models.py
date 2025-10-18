from typing import Generic, TypeVar

from pydantic import Field
from pydantic.generics import GenericModel

T = TypeVar("T")


class GenericResponse(GenericModel, Generic[T]):
    status: str = Field(..., example="SUCCESS")
    message: str = Field(..., example="Request successful")
    data: T | None = None

    class Config:
        arbitrary_types_allowed = True
