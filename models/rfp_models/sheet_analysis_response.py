from typing import List, Optional
from pydantic import BaseModel, Field

class Column(BaseModel):
    name: str
    is_question_column: bool = False
    is_answer_column: bool = False

class SheetDetail(BaseModel):
    sheet_name: str
    is_sheet_selected: bool = False
    columns: List[Column]
    column_row_number: int

class SheetAnalysisResponse(BaseModel):
    status: str
    data: dict = Field(..., description="Contains sheets_details")
    message: str

    class Config:
        json_schema_extra = {
            "example": {
                "status": "SUCCESS",
                "data": {
                    "sheets_details": [
                        {
                            "sheet_name": "5.1 Loyalty Management",
                            "is_sheet_selected": False,
                            "columns": [
                                {
                                    "name": "Loyalty Requirement",
                                    "is_question_column": False,
                                    "is_answer_column": False
                                }
                            ],
                            "column_row_number": 1
                        }
                    ]
                },
                "message": "Sheet analysis completed successfully"
            }
        } 