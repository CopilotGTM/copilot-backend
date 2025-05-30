from pydantic import BaseModel, Field
from typing import List

class SlideItem(BaseModel):
    """Represents a single slide in a presentation outline.
    
    Attributes:
        slide_number (int): The sequential number of the slide in the presentation
        slide_title (str): The title/heading of the slide
        description (str): A brief explanation of the slide's purpose and importance
    """

    slide_number: int = Field(..., description="The number of the slide")
    slide_title: str = Field(..., description="The title of the slide")
    description: str = Field(..., description="Why this slide is important")


class SlideOutline(BaseModel):
    """Represents a complete outline of slides for a presentation.
    
    Attributes:
        slide_outline (List[SlideItem]): An ordered list of slides that make up the presentation
    """

    slide_outline: List[SlideItem]


class SolutionNarration(BaseModel):
    """Represents the narrative content for a solution presentation.
    
    Attributes:
        solutioning_narration (str): The detailed narrative content explaining the solution
    """

    solutioning_narration: str = Field(..., description="The narrative content for the solution presentation")
