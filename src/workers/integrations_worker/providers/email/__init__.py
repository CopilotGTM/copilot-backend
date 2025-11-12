"""Email integration providers."""

from .outlook_email import fetch_folders as outlook_fetch_folders
from .outlook_email import fetch_messages as outlook_fetch_messages

__all__ = [
    "outlook_fetch_folders",
    "outlook_fetch_messages",
]
