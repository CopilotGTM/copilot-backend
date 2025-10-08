from typing import List, Optional
from pydantic import BaseModel, Field
from uuid import UUID


class Column(BaseModel):
    name: str
    is_question_column: Optional[bool] = Field(default=False)
    is_answer_column: Optional[bool] = Field(default=False)


class SheetDetail(BaseModel):
    sheet_name: str
    is_sheet_selected: bool = False
    columns: List[Column]
    column_row_number: int


class SheetSelection(BaseModel):
    sheet_name: str
    question_column: str
    answer_column: str


class SheetSelectionInput(BaseModel):
    session_id: UUID = Field(..., description="Conversation thread ID")
    system_message_id: UUID = Field(..., description="Unique message ID")
    sheets_details: List[SheetSelection] = Field(
        ..., description="List of sheet details with question and answer columns"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "session_id": "123e4567-e89b-12d3-a456-426614174000",
                "message_id": "123e4567-e89b-12d3-a456-426614174001",
                "sheets_details": [
                    {
                        "sheet_name": "5.1 Loyalty Management",
                        "question_column": "Description",
                        "answer_column": "Remarks",
                    }
                ],
                "message": "Please analyze the loyalty management sheet",
            }
        }


class SheetAnalysisResponse(BaseModel):
    status: str
    data: dict = Field(..., description="Contains sheets_details")
    message: str

    class Config:
        json_schema_extra = {
            "example": {
                "status": "SUCCESS",
                "data": {
                    "message_id": "123e4567-e89b-12d3-a456-426614174000",
                    "sheets_details": [
                        {
                            "sheet_name": "5.1 Loyalty Management",
                            "is_sheet_selected": False,
                            "columns": [
                                {
                                    "name": "Loyalty Requirement",
                                    "is_question_column": False,
                                    "is_answer_column": False,
                                }
                            ],
                            "column_row_number": 1,
                        }
                    ],
                },
                "message": "Sheet analysis completed successfully",
            }
        }
