"""Communication-related agent tools.

Tools for accessing email, messaging, and chat attachment information.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.tools.communication.chat_attachments import (
        create_list_chat_attachments,
        create_read_chat_attachment,
    )
    from src.agents.tools.communication.email_drafts import (
        create_save_email_draft,
        create_send_email_draft,
    )
    from src.agents.tools.communication.fetch_teams_chat_messages import (
        create_fetch_teams_chat_messages,
    )
    from src.agents.tools.communication.get_email_details import create_get_email_details
    from src.agents.tools.communication.list_channel_messages import (
        create_list_deal_channel_messages,
    )
    from src.agents.tools.communication.list_emails import create_list_deal_emails
    from src.agents.tools.communication.mailbox_emails import (
        create_list_connected_email_accounts,
        create_search_mailbox_emails,
    )
    from src.agents.tools.communication.send_email import create_send_email

_EXPORTS = {
    "create_read_chat_attachment": (
        "src.agents.tools.communication.chat_attachments",
        "create_read_chat_attachment",
    ),
    "create_list_chat_attachments": (
        "src.agents.tools.communication.chat_attachments",
        "create_list_chat_attachments",
    ),
    "create_list_deal_emails": (
        "src.agents.tools.communication.list_emails",
        "create_list_deal_emails",
    ),
    "create_get_email_details": (
        "src.agents.tools.communication.get_email_details",
        "create_get_email_details",
    ),
    "create_list_connected_email_accounts": (
        "src.agents.tools.communication.mailbox_emails",
        "create_list_connected_email_accounts",
    ),
    "create_search_mailbox_emails": (
        "src.agents.tools.communication.mailbox_emails",
        "create_search_mailbox_emails",
    ),
    "create_list_deal_channel_messages": (
        "src.agents.tools.communication.list_channel_messages",
        "create_list_deal_channel_messages",
    ),
    "create_fetch_teams_chat_messages": (
        "src.agents.tools.communication.fetch_teams_chat_messages",
        "create_fetch_teams_chat_messages",
    ),
    "create_send_email": (
        "src.agents.tools.communication.send_email",
        "create_send_email",
    ),
    "create_save_email_draft": (
        "src.agents.tools.communication.email_drafts",
        "create_save_email_draft",
    ),
    "create_send_email_draft": (
        "src.agents.tools.communication.email_drafts",
        "create_send_email_draft",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, export_name = _EXPORTS[name]
    module = import_module(module_name)
    return getattr(module, export_name)
