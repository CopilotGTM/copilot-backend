from pydantic import BaseModel, Field
from typing import List


class ReplaceText(BaseModel):
    """
    Represents a single find/replace operation.
    """
    previous_text: str = Field(
        ...,
        description="The exact substring to search for and replace."
    )
    updated_text: str = Field(
        ...,
        description="The new string to substitute in place of the original."
    )


class TextMapping(BaseModel):
    """
    A batch of text replacements, plus whether to update the slide.
    """
    replacements: List[ReplaceText] = Field(
        ...,
        description="List of individual text replacement instructions."
    )
    slide_required: bool = Field(
        False,
        description="Whether the corresponding slide should also be updated."
    )
    reasoning: str = Field(
        ...,
        description="Why we are using this slide"
    )
    slide_summary: str = Field(
        ...,
        description="This new slide summary with updated text"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "replacements": [
                    {"previous_text": "Foo", "updated_text": "Bar"},
                    {"previous_text": "2024", "updated_text": "2025"}
                ],
                "slide_required": True,
                "reasoning": "Reasoning why we are using"
            }
        }
