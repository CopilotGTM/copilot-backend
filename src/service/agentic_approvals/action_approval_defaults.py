"""Default approval policy for agent-triggered side effects.

The defaults are the product baseline used when a user has not saved settings.
They also mark locked tools such as external email sending, where the runtime
must require confirmation regardless of user preference.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionApprovalDefault:
    """Static metadata for one configurable agent action."""

    tool_name: str
    label: str
    category: str
    approval_required: bool
    editable: bool
    locked_reason: str | None = None
    crm_write_scope: str | None = None


ACTION_APPROVAL_DEFAULTS: dict[str, ActionApprovalDefault] = {
    "create_task": ActionApprovalDefault(
        tool_name="create_task",
        label="Create task",
        category="crm",
        approval_required=False,
        editable=True,
        crm_write_scope="tasks:create",
    ),
    "add_deal_note": ActionApprovalDefault(
        tool_name="add_deal_note",
        label="Add deal note",
        category="crm",
        approval_required=False,
        editable=True,
    ),
    "create_deal": ActionApprovalDefault(
        tool_name="create_deal",
        label="Create deal",
        category="crm",
        approval_required=True,
        editable=True,
        crm_write_scope="deals:create",
    ),
    "create_lead": ActionApprovalDefault(
        tool_name="create_lead",
        label="Create lead",
        category="crm",
        approval_required=True,
        editable=True,
        crm_write_scope="leads:create",
    ),
    "create_account": ActionApprovalDefault(
        tool_name="create_account",
        label="Create account",
        category="crm",
        approval_required=True,
        editable=True,
        crm_write_scope="accounts:create",
    ),
    "create_contact": ActionApprovalDefault(
        tool_name="create_contact",
        label="Create contact",
        category="crm",
        approval_required=True,
        editable=True,
        crm_write_scope="contacts:create",
    ),
    "add_stakeholder": ActionApprovalDefault(
        tool_name="add_stakeholder",
        label="Add stakeholder",
        category="crm",
        approval_required=True,
        editable=True,
        crm_write_scope="deal_participants:create",
    ),
    "create_reminder": ActionApprovalDefault(
        tool_name="create_reminder",
        label="Create reminder",
        category="reminders",
        approval_required=True,
        editable=True,
    ),
    "send_calendar_invite": ActionApprovalDefault(
        tool_name="send_calendar_invite",
        label="Send calendar invite",
        category="calendar",
        approval_required=True,
        editable=True,
    ),
    "send_email": ActionApprovalDefault(
        tool_name="send_email",
        label="Send email",
        category="communication",
        approval_required=True,
        editable=False,
        locked_reason="External email sending is always approval-gated.",
    ),
    "save_email_draft": ActionApprovalDefault(
        tool_name="save_email_draft",
        label="Save email draft",
        category="communication",
        approval_required=False,
        editable=True,
    ),
    "send_email_draft": ActionApprovalDefault(
        tool_name="send_email_draft",
        label="Send email draft",
        category="communication",
        approval_required=True,
        editable=False,
        locked_reason="External email sending is always approval-gated.",
    ),
    "update_calendar_meeting": ActionApprovalDefault(
        tool_name="update_calendar_meeting",
        label="Update calendar meeting",
        category="calendar",
        approval_required=True,
        editable=True,
    ),
    "cancel_calendar_meeting": ActionApprovalDefault(
        tool_name="cancel_calendar_meeting",
        label="Cancel calendar meeting",
        category="calendar",
        approval_required=True,
        editable=False,
        locked_reason="Meeting cancellation is always approval-gated.",
    ),
}

LOCKED_APPROVAL_TOOLS = {
    tool_name for tool_name, config in ACTION_APPROVAL_DEFAULTS.items() if not config.editable
}


def get_action_approval_default(tool_name: str) -> ActionApprovalDefault | None:
    """Return the registered default for a known agent action tool."""

    return ACTION_APPROVAL_DEFAULTS.get(tool_name)
