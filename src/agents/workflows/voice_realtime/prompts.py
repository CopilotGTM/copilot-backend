"""Prompt templates for the realtime voice agent."""

import os

from src.agents.workflows.general.prompts.general_prompt import get_general_prompt


def _without_inactive_skills_section(prompt: str) -> str:
    """Remove Agno skill-loading directions that the Azure sideband path cannot execute."""
    before_skills, skills_marker, after_skills = prompt.partition("## Skills")
    if not skills_marker:
        return prompt
    _skills_body, next_marker, after_next_section = after_skills.partition(
        "## Date and Time Grounding"
    )
    if not next_marker:
        return prompt
    prompt_without_section = f"{before_skills}{next_marker}{after_next_section}"
    return (
        prompt_without_section.replace(
            "Identify and use the correct tools and skills.",
            "Identify and use the correct available tools and routing rules.",
        )
        .replace(
            "For workflow or action intents, check and load the matching skill before executing "
            "the domain workflow.",
            "For workflow or action intents, follow the matching voice tool routing rule before "
            "executing the domain workflow.",
        )
        .replace(
            "Follow any response schema explicitly required by an active skill, tool, or "
            "application runtime.",
            "Follow any response schema explicitly required by a tool or application runtime.",
        )
    )


def get_voice_realtime_system_prompt(org_name: str | None) -> str:
    """Reuse the general prompt baseline, trimmed for spoken realtime sessions."""
    base_prompt = _without_inactive_skills_section(get_general_prompt(org_name))
    trimmed_prompt = base_prompt.split("## Agent Task Tracking")[0].strip()
    public_web_guidance = ""
    if os.getenv("VOICE_REALTIME_PUBLIC_WEB_TOOLS_ENABLED", "false").lower() == "true":
        public_web_guidance = """
- Use `search_internet` to discover public sources and `crawl_webpage` only when an exact public
  URL is known. Never send private CRM, email, transcript, or attachment content to either tool.
""".rstrip()

    voice_suffix = f"""
---

## Voice Tool Routing

- Use available read tools silently before asking for deal, meeting, mailbox, or person details
  that can be resolved from current records.
{public_web_guidance}
- Use `get_deal_memory` for accumulated deal context and `list_deal_stakeholders` for the buying
  committee or one external stakeholder.
- Resolve "our team", "our rep", or "our colleague" with `search_crm_users`. For meeting edits,
  pass a structured attendee with `scope=team`; never guess the person's email.
- Use `list_connected_email_accounts` when the mailbox is ambiguous, then
  `search_mailbox_emails` to find the exact message or thread. Read attachments only with IDs from
  `get_email_details` and the same mailbox or deal scope.
- New, generated, materially rewritten, or explicitly "send directly" email defaults to
  `save_email_draft`, which never sends.
  Use `send_email_draft` only after showing the exact saved draft and receiving one unambiguous
  confirmation. Replay the exact draft ID and content hash unchanged.
- Use `update_calendar_meeting` for an existing event; never create a replacement. Infer attendee
  `add`, `remove`, or `replace` from meaning and preserve the existing roster for add or remove.
- Meeting updates, cancellations, and draft sends require preview first. Call with
  `confirmed=false`, speak the returned review, then replay the exact reviewed payload with
  `confirmed=true` only after the user confirms that single pending action.
- An ambiguous "yes" is not approval when more than one action is pending. Never blindly retry an
  external mutation after an unknown, timeout, or partial result.

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
