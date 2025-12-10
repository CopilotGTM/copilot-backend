from __future__ import annotations

from textwrap import dedent

from src.agents.common.prompt_sections.formatting import MARKDOWN_FORMATTING_RULES

TOOLING_GUIDANCE = dedent(
    f"""
    Tooling guidance:
    - InternalKnowledgeBase: Search Our Organization's internal documents. Returns citations with internal links (kb://org/{{org_id}}/doc/{{doc_id}}#page={{page}}). Always prefer this first for company-specific information.
    - InternetSearch: Free web search powered by DuckDuckGo (no API key required). Use when the user needs up-to-date or external public facts not available internally. Cite with the returned URLs.
    - When presenting search results or tool outputs, format all content using proper Markdown:
{MARKDOWN_FORMATTING_RULES}
    - If neither tool returns helpful results, say so explicitly and provide the best guidance you can from existing context.
    """
)
