from pydantic import BaseModel, Field
from typing import Literal


class OutputAnswer(BaseModel):
    """The output answer model"""

    answer: str = Field(description="The final answer to the user's question")
    confidence_score: float = Field(description="The confidence score of the answer")
    confidence_level: Literal["high", "medium", "low"] = Field(description="The confidence level of the answer")
