from pydantic import BaseModel, Field


class QueryReformulation(BaseModel):
    """The reformulated query model"""

    reformulated_query: str = Field(description="Reformulated the query for next steps with better context of history")
    query_context: str = Field(description="The context of the query")
    is_greeting: bool = Field(description="Whether the reformulated query is a greeting")
    greeting_response: str = Field(description="Greeting response")


class QueryPlanning(BaseModel):
    """A list of objects where each object represents one step and contains required tool."""

    plan: str = Field(description="List of Steps that need to be followed by the agent to complete the task")