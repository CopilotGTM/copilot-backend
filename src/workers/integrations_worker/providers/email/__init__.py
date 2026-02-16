"""Email integration providers."""

from .gmail_email import FetchMessagesResult as GmailFetchMessagesResult
from .gmail_email import fetch_labels as gmail_fetch_labels
from .gmail_email import fetch_messages as gmail_fetch_messages
from .outlook_email import FetchMessagesResult
from .outlook_email import fetch_folders as outlook_fetch_folders
from .outlook_email import fetch_messages as outlook_fetch_messages

__all__ = [
    "FetchMessagesResult",
    "GmailFetchMessagesResult",
    "gmail_fetch_labels",
    "gmail_fetch_messages",
    "outlook_fetch_folders",
    "outlook_fetch_messages",
]
