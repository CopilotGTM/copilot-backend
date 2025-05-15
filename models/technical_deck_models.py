# models/technical_deck.py

from uuid import UUID
from pydantic import BaseModel, Field


class TemplateDetails(BaseModel):
    """
    A data model representing details of a selected template.
    """

    template_id: UUID = Field(..., description="The unique identifier of the selected template.")
    template_name: str = Field(..., description="The name of the template.")
    template_thumbnail_url: str = Field(..., description="The URL of the template's thumbnail image.")
    template_description: str = Field(..., description="A brief description of the template.")


class TemplateSelection(BaseModel):
    """
    A data model representing the selection of a template.
    """

    session_id: UUID = Field(..., description="The unique identifier of the session.")
    message_id: UUID = Field(..., description="The unique identifier of the message.")
    template_details: TemplateDetails = Field(..., description="Details of the selected template.")

    class Config:
        from_attributes = True


class GenerateTechnicalDeckRequest(BaseModel):
    """
    A data model representing a request to generate a technical deck.
    """

    session_id: UUID = Field(..., description="The unique identifier of the session.")
    message_id: UUID = Field(..., description="The unique identifier of the message.")

    class Config:
        from_attributes = True
