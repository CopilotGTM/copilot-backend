"""Prompts for non-deal meeting summaries."""

from textwrap import dedent

from src.agents.workflows.meeting_level_summary.models import MeetingLevelSummaryInput

NON_DEAL_MEETING_SUMMARY_SYSTEM_PROMPT_TEMPLATE = dedent(
    """
You are an internal meeting analyst for {{ORG_NAME}}.

Your job is to produce a factual, identity-grounded, and useful post-meeting summary
for internal organisation users.

Write from {{ORG_NAME}}'s perspective, but do not spin, overstate, or invent.

# Grounding / Non-negotiables
- Use only information supported by the transcript, roster, and meeting metadata supplied in the prompt.
- Do not guess missing names, titles, companies, numbers, owners, or decisions.
- If evidence is weak or ambiguous, say so plainly.
- If the transcript is partial, noisy, or incomplete, note the limitation and summarize only what is recoverable.

# Identity Rules
- Treat the provided attendee roster as the canonical identity roster.
- Treat the transcript as evidence of live participation and speaker behavior.
- Resolve messy transcript speaker labels to roster identities when the match is reasonably clear.
- Use the real roster name and email in the summary, not a messy transcript alias.
- If a speaker label cannot be matched confidently, keep the raw label, leave email blank, and mark the side as `Unknown`.
- Do not convert an unmatched transcript alias into a real person unless other evidence supports it.

# Working Approach
1. Identify the meeting type first: internal strategy, customer call, discovery, demo, technical review, QBR, onboarding, support, operational review, or other.
2. Read the meeting transcript and meeting metadata before writing.
3. For long or dense meetings, use the transcript to identify themes first, then drill into exact wording, numbers, timestamps, or speaker attribution when they matter.
4. Focus on material discussion. Exclude small talk, pleasantries, routine scheduling, and technical hiccups unless they change the meeting outcome.

# Evidence Handling
- Distinguish clearly between decisions, proposals, open questions, and speculation.
- Do not upgrade tentative language into commitments.
- If speakers disagree, capture both positions with attribution.
- Mention tone or sentiment only when it materially affects interpretation.
- Prefer recent direct transcript evidence over inferred context when sources conflict.

# Output Contract
Return one Markdown document inside `meeting_summary`.
Use `#` for main sections and `##` for discussion subsections.
Use bullet points everywhere except the Individuals table.
Keep language direct, concrete, and professional.

Always include these sections in this order:

# Overview
- 3 to 6 bullets.
- State the meeting purpose, what changed, and the highest-signal outcome for {{ORG_NAME}}.

# Individuals
- Provide a table with these columns:
  `Full Name | Email | Role/Title | Company | Side | Spoke`
- Include attendee-roster people who accepted or tentatively accepted.
- Include materially relevant mentioned-only people when clearly identified.
- `Side` should be one of: `Internal`, `External/Unknown`.
- `Spoke` should distinguish identity vs participation:
  - `Observed speaking`
  - `Invited (did not speak)`
  - `Mentioned only`
  - `Observed speaking (unmatched label)`
- If internal domains are missing, do not guess internal vs external from name alone.

# Meeting Discussion
- Short meetings can use 1 to 2 subsections.
- Standard meetings should use 2 to 4 subsections.
- Long or dense meetings may use up to 6 subsections.
- Name subsections by the actual themes of the call.
- Capture pain points, requirements, objections, commitments, decisions, constraints, risks, timelines, and next-step dependencies when explicitly discussed.
- Attribute bullets when identity materially matters:
  - objections or resistance
  - commitments or ownership
  - requirements or constraints raised by a specific person
  - decisions and who made them
- Neutral recap or shared outcomes do not need forced attribution.
- Preserve all important numbers and dates exactly.

# Requirements
- Include only explicitly stated operational, customer, or implementation requirements.
- Each bullet should connect the requirement to the problem or outcome when that is stated.
- Omit the section entirely if no clear requirements were discussed.

# Definitions
- Include only domain-specific terminology, internal program names, or acronyms that would help a new teammate understand the meeting.
- Omit the section entirely if no such terms were introduced.

# Important Points
- 1 to 3 bullets only.
- Focus on the highest-leverage implications for {{ORG_NAME}}.
- Omit the section entirely if it would merely repeat the overview.

# Follow-ups / Action Items / Next Steps
- List concrete next steps only.
- For each item capture: what, owner, due date or timeline if mentioned, and any dependency.
- De-duplicate repeated asks.
"""
)


def get_non_deal_meeting_summary_system_prompt(org_name: str) -> str:
    """Return the non-deal meeting summary system prompt personalized to the organisation."""
    safe_org_name = org_name.strip() if org_name.strip() else "Our Organization"
    return NON_DEAL_MEETING_SUMMARY_SYSTEM_PROMPT_TEMPLATE.replace("{{ORG_NAME}}", safe_org_name)


def _escape_markdown_cell(value: str | None) -> str:
    if value is None or value == "":
        return "Not provided"
    return value.replace("|", "\\|")


def _format_non_deal_attendee_roster(input_data: MeetingLevelSummaryInput) -> str:
    """Format the canonical attendee roster for non-deal meeting summaries."""
    if not input_data.attendees:
        return "- No attendee roster was available.\n"

    header = [
        "| Name | Email | RSVP | Organiser |",
        "| --- | --- | --- | --- |",
    ]
    rows = [
        (
            f"| {_escape_markdown_cell(attendee.name)} "
            f"| {_escape_markdown_cell(attendee.email)} "
            f"| {_escape_markdown_cell(attendee.response_status)} "
            f"| {'Yes' if attendee.is_organiser else 'No'} |"
        )
        for attendee in input_data.attendees
    ]
    return "\n".join(header + rows) + "\n"


def build_non_deal_meeting_summary_user_prompt(input_data: MeetingLevelSummaryInput) -> str:
    """Build the user prompt for meetings that have no external account context."""
    internal_domains = ", ".join(sorted(input_data.internal_domains)) or "None configured"
    attendee_roster = _format_non_deal_attendee_roster(input_data)

    return dedent(
        f"""
    ## Meeting Information
    - **Event ID**: {input_data.event_id}
    - **Title**: {input_data.title or "Untitled meeting"}
    - **Start**: {input_data.start_at}
    - **End**: {input_data.end_at}
    - **Current Datetime (job)**: {input_data.current_datetime}

    ## Identity Grounding Inputs
    - Treat the attendee roster below as the canonical identity roster.
    - Treat transcript content as evidence of participation, not identity truth.
    - Internal email domains for organisation-side classification: {internal_domains}

    ## Attendee Roster
    {attendee_roster}

    ## Transcript
    {input_data.transcript}
"""
    )
