from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class GenericResponse(BaseModel, Generic[T]):
    status: str = Field(examples=["SUCCESS"])
    message: str = Field(examples=["Request successful"])
    code: str | None = None
    data: T | None = None

    # Pydantic v2 config
    model_config = {"arbitrary_types_allowed": True}
