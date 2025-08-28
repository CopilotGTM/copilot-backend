from agno.models.azure import AzureOpenAI
from agno.agent import Agent

image_summary_agent: Agent = Agent(
    model=AzureOpenAI(id="gpt-4o", api_version="2024-12-01-preview"),
    description=(
        """Analyze and summarize the image in detail for RAG purposes. Focus on:

        1. Visual Content:
           - Main subject(s) and their arrangement
           - Colors, shapes, and visual elements
           - Text or labels present
           - Any charts, graphs, or data visualizations

        2. Context and Purpose:
           - Type of image (chart, diagram, photo, illustration)
           - Business or technical context
           - Key message or information conveyed

        3. Technical Details:
           - Data visualization type (if applicable)
           - Trends or patterns shown
           - Scale or units of measurement
           - Any annotations or callouts

        4. Business Relevance:
           - Industry or domain context
           - Target audience
           - Key insights or takeaways

        Provide a structured summary that captures all these aspects while maintaining clarity and objectivity.
        Focus on details that would be valuable for retrieval and reuse in business contexts."""
    ),
    debug_mode=False,
)
