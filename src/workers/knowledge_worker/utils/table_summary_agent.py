from agno.models.azure import AzureOpenAI
from agno.agent import Agent

table_summary_agent: Agent = Agent(
    model=AzureOpenAI(id="gpt-4o", api_version="2024-12-01-preview"),
    description=(
        "Summarize this table in 500 tokens for retrieval use. "
        "If row and column counts are relevant, include them. Otherwise, focus on summarizing key content. "
        "Highlight two specific insights, trends, or patterns that help identify the table's subject and potential use."
    ),
    debug_mode=False,
)
