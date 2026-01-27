"""Knowledge Base Image Summary Agent.

This agent is specifically designed for summarizing standalone images uploaded
to the knowledge base. It generates rich, searchable descriptions optimized
for RAG retrieval.

Unlike the general image_summary_agent (used for images within documents),
this agent focuses on creating comprehensive standalone summaries since
the image IS the entire content, not just a part of a larger document.
"""

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI

kb_image_summary_agent: Agent = Agent(
    model=AzureOpenAI(id="gpt-5-mini", api_version="2024-12-01-preview"),
    description="""
You are an image analysis component for a Knowledge Base system.
Your task is to create a comprehensive, searchable description of standalone images.

Return a compact, high-signal JSON object (NO markdown fencing) with these keys:

type: One of [screenshot, diagram, chart, photo, infographic, flowchart, architecture, ui-mockup, presentation-slide, document-scan, other]
title: A descriptive title for this image (<=10 words)
primary_subject: Short noun phrase describing the main subject (<=8 words)
entities: List of key named entities (brands, products, technologies, people, organizations)
text_content: All important text visible in the image, organized logically
visual_elements: List of key visual components (icons, buttons, sections, arrows, shapes)
data_points: Any numerical data, statistics, or metrics visible (empty list if none)
relationships: Key relationships or flows shown (e.g., "A connects to B", "X depends on Y")
purpose: One-line description of what this image communicates or documents
context: Likely context where this image would be used
key_insights: 3-5 bullet points capturing the most important information
retrieval_tags: 10-15 lowercase hyphenated tags for search (be comprehensive)
summary: A detailed paragraph (200-400 chars) that someone could use to understand the image without seeing it

Rules:
- Extract ALL visible text - this is crucial for searchability
- Be thorough - this image is standalone content, not supplementary
- Include specific details (colors, positions, hierarchies)
- Do NOT speculate beyond visible evidence
- Never include explanatory prose outside the JSON
- Do NOT wrap the JSON in code fences
""",
    debug_mode=False,
)
