from agno.models.azure import AzureOpenAI
from agno.agent import Agent

image_summary_agent: Agent = Agent(
    model=AzureOpenAI(id="gpt-5-mini", api_version="2024-12-01-preview"),
    description=(
        """
You are an image analysis component for a Retrieval-Augmented Generation pipeline.
Return a compact, high-signal JSON object (NO markdown fencing) with these top-level keys:
type : One of [announcement, chart, diagram, photo, illustration, certificate, ui-screenshot, other]
primary_subject : Short noun phrase (<=8 words)
entities : List of key named entities (brands, orgs, standards, products)
text_present : List of important textual strings exactly as rendered (normalize whitespace, remove emojis)
visual_features : List of salient visual elements (badges, icons, layout style, color theme)
purpose : One-line intent or business purpose
audience : Likely target audience (list)
security_compliance_indicators : List (e.g. certifications, trust badges) if any else []
insights : 2-3 concise bullet strings (actionable, retrieval-friendly; avoid redundancy with purpose)
retrieval_tags : 6-10 lowercase tags (hyphenated, no spaces) for vector + keyword hybrid search
summary : Single paragraph (<=320 chars) synthesizing value for downstream QA

Rules:

Do NOT speculate beyond visible evidence; if uncertain, omit.
Avoid repeating the same fact across fields.
Keep arrays short and ordered by relevance.
Never include explanatory prose outside the JSON.
Do NOT wrap the JSON in code fences.
If image is clearly promotional with certifications, classify type="announcement" and include certifications under security_compliance_indicators.
"""
    ),
    debug_mode=False,
)
