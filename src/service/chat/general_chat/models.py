from pydantic import BaseModel, Field


class QueryReformulation(BaseModel):
    """The reformulated query model"""

    is_greeting: bool = Field(description="true In case of greeting")
    greeting_response: str = Field(description="Greeting response")
    category: str = Field(description="Classification of Question")


class QueryPlanning(BaseModel):
    """A list of objects where each object represents one step and contains required tool."""

    plan: str = Field(description="List of Steps that need to be followed by the agent to complete the task")
