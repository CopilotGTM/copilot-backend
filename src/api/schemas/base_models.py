from pydantic import BaseModel, Field
from pydantic.generics import GenericModel
from typing import TypeVar, Generic, Optional, Any

T = TypeVar("T")


class GenericResponse(GenericModel, Generic[T]):
    status: str = Field(..., example="SUCCESS")
    message: str = Field(..., example="Request successful")
    data: Optional[T] = None

    class Config:
        arbitrary_types_allowed = True
