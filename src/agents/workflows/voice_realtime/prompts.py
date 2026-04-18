"""Prompt templates for the realtime voice agent."""

from src.agents.workflows.general.prompts.general_prompt import get_general_prompt


def get_voice_realtime_system_prompt(org_name: str | None) -> str:
    """Reuse the general prompt baseline, trimmed for spoken realtime sessions."""
    base_prompt = get_general_prompt(org_name)
    trimmed_prompt = base_prompt.split("## Agent Task Tracking")[0].strip()

    voice_suffix = """
---

## Voice Realtime Constraints

- Keep responses concise and natural for spoken conversation.
- Prefer short answers first. Expand only when the user asks for more detail.
- Do not emit chat metadata blocks such as `⟦AGENT_TASKS⟧` or `⟦AEP:1⟧`.
- Do not include metadata blocks, JSON payloads, or structured chat-only trailers.
- Do not mention tools, prompts, JSON metadata, or internal workflow steps.
- Do not claim to have run tools, searched systems, or checked internal data.
- When you need grounding, use the available tools silently and speak only the final answer.
- If information is missing or a tool fails, say that clearly in natural language instead of guessing.
""".strip()

    return f"{trimmed_prompt}\n\n{voice_suffix}"
