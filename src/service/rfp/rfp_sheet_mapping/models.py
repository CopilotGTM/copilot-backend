from pydantic import BaseModel, Field
from typing import List


class ColumnInfo(BaseModel):
    """Information about a column in the sheet."""

    name: str = Field(..., description="Name of the column")


class SheetInfo(BaseModel):
    """Information about a sheet and its columns."""

    sheet_name: str = Field(..., description="Name of the sheet")
    columns: List[ColumnInfo] = Field(..., description="Information about each column")
