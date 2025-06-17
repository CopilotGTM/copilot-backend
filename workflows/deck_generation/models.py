from typing import List

from pydantic import BaseModel, Field


class ReplaceText(BaseModel):
    """
    Represents a single find/replace operation.
    """

    id: str = Field(..., description="Unique identifier for the text element to be replaced.")
    previous_text: str = Field(..., description="The exact substring to search for and replace.")
    updated_text: str = Field(..., description="The new string to substitute in place of the original.")


class TextMapping(BaseModel):
    """
    A batch of text replacements, plus whether to update the slide.
    """

    replacements: List[ReplaceText] = Field(..., description="List of individual text replacement instructions.")
    slide_required: bool = Field(False, description="Whether the corresponding slide should also be updated.")
    slide_summary: str = Field(
        ...,
        description="Explanation of this new slide and its purpose for the meeting. Keep it concise. 20-25 words max.",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "replacements": [
                    {"previous_text": "Foo", "updated_text": "Bar"},
                    {"previous_text": "2024", "updated_text": "2025"},
                ],
                "slide_required": True,
                "reasoning": "Reasoning why we are using this slide.",
                "slide_summary": "Summary of the updated slide text.",
            }
        }
